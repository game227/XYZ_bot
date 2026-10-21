"""Teacher authentication (TZ §7.1, §7.2, §7.6). OWNER: AUTH.

- Passwords: Argon2id (argon2-cffi), hashed off the event loop. Plain passwords are never
  stored or logged; the audit log gets only the outcome.
- Login happens only in the Mini App form (the password never appears in the bot chat).
- Brute force: 5 failed attempts within 15 minutes, per login and per Telegram ID, lock
  login for 15 minutes (Redis). If Redis is unreachable, login is refused (fail closed).
- Admin operations on teacher accounts (create / block / reset password / reset binding /
  course assignments) live here too, so bot handlers and tests share one implementation.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import string
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import (
    Course,
    TeacherAccount,
    TeacherCourseAssignment,
    TeacherSessionEndReason,
    User,
    UserRole,
)
from app.services import audit, teacher_sessions
from app.services.errors import AccessDenied, Conflict, InvalidInput, NotFound, ServiceError
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- passwords

_hasher = PasswordHasher()  # argon2id, RFC 9106 "low memory" profile (t=3, m=64 MiB, p=4)
# Verified against unknown logins so response time does not reveal whether a login exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_.]{2,31}$")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def normalize_login(login: str) -> str:
    return (login or "").strip().lower()


def validate_login(login: str) -> str:
    login = normalize_login(login)
    if not LOGIN_RE.fullmatch(login):
        raise InvalidInput(
            "Login 3–32 belgidan iborat bo‘lishi kerak: kichik lotin harflari, raqamlar, “_” va “.”."
        )
    return login


def validate_new_password(password: str, *, login: str | None = None) -> None:
    """Server-side password policy. Raises InvalidInput with an Uzbek explanation."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise InvalidInput(f"Parol kamida {PASSWORD_MIN_LENGTH} ta belgidan iborat bo‘lishi kerak.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise InvalidInput(f"Parol {PASSWORD_MAX_LENGTH} ta belgidan oshmasligi kerak.")
    if password != password.strip():
        raise InvalidInput("Parol bo‘sh joy bilan boshlanmasligi yoki tugamasligi kerak.")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise InvalidInput("Parolda kamida bitta harf va bitta raqam bo‘lishi kerak.")
    if login and normalize_login(login) in password.lower():
        raise InvalidInput("Parol tarkibida login bo‘lmasligi kerak.")


_TEMP_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0OoIl1")


def generate_temp_password(length: int = 14) -> str:
    """Random one-time password handed to the teacher by an admin (must be changed on first login)."""
    while True:
        pwd = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in pwd) and any(c.isalpha() for c in pwd):
            return pwd


# --------------------------------------------------------------------------- rate limit

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60


class RedisLike(Protocol):
    async def get(self, name: str) -> object: ...
    async def set(self, name: str, value: object, ex: int | None = None, nx: bool = False) -> object: ...
    async def incr(self, name: str) -> int: ...
    async def delete(self, *names: str) -> int: ...
    async def ttl(self, name: str) -> int: ...


class LoginLocked(ServiceError):
    default_message = "Juda ko‘p noto‘g‘ri urinish. Keyinroq qayta urinib ko‘ring."

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(int(retry_after), 1)
        minutes = max((self.retry_after + 59) // 60, 1)
        super().__init__(f"Juda ko‘p noto‘g‘ri urinish. {minutes} daqiqadan so‘ng qayta urinib ko‘ring.")


class LoginUnavailable(ServiceError):
    default_message = "Kirish vaqtincha imkonsiz. Birozdan so‘ng qayta urinib ko‘ring."


def _subject_key(kind: str, value: str) -> str:
    # Logins are user input: hash them so arbitrary text never becomes part of a Redis key.
    digest = hashlib.sha256(value.encode()).hexdigest()[:32]
    return f"{kind}:{digest}"


class LoginRateLimiter:
    """Fixed-window failure counter + lock, per subject ("login:<h>", "tg:<id>")."""

    def __init__(
        self,
        redis: RedisLike,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: int = LOGIN_WINDOW_SECONDS,
        lock_seconds: int = LOGIN_LOCK_SECONDS,
        prefix: str = "au:login",
    ) -> None:
        self.redis = redis
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self.prefix = prefix

    @staticmethod
    def subjects(login: str, telegram_id: int) -> list[str]:
        return [_subject_key("login", normalize_login(login)[:64]), f"tg:{int(telegram_id)}"]

    def _fail_key(self, subject: str) -> str:
        return f"{self.prefix}:fail:{subject}"

    def _lock_key(self, subject: str) -> str:
        return f"{self.prefix}:lock:{subject}"

    async def check(self, subjects: list[str]) -> None:
        """Raise LoginLocked if any subject is locked; LoginUnavailable if Redis fails."""
        try:
            for subject in subjects:
                if await self.redis.get(self._lock_key(subject)) is not None:
                    ttl = await self.redis.ttl(self._lock_key(subject))
                    raise LoginLocked(ttl if ttl and ttl > 0 else self.lock_seconds)
        except LoginLocked:
            raise
        except Exception as exc:
            logger.error("login rate limiter unavailable: %s", type(exc).__name__)
            raise LoginUnavailable() from exc

    async def register_failure(self, subjects: list[str]) -> bool:
        """Count a failure; returns True if this failure locked any subject."""
        locked = False
        try:
            for subject in subjects:
                key = self._fail_key(subject)
                await self.redis.set(key, 0, ex=self.window_seconds, nx=True)
                count = await self.redis.incr(key)
                if count >= self.max_failures:
                    await self.redis.set(self._lock_key(subject), 1, ex=self.lock_seconds)
                    await self.redis.delete(key)
                    locked = True
        except Exception as exc:
            logger.error("login rate limiter unavailable: %s", type(exc).__name__)
            raise LoginUnavailable() from exc
        return locked

    async def reset(self, subjects: list[str]) -> None:
        try:
            await self.redis.delete(*[self._fail_key(s) for s in subjects])
        except Exception:
            logger.warning("login rate limiter reset failed", exc_info=True)


@lru_cache
def get_redis() -> RedisLike:
    from redis.asyncio import Redis

    return Redis.from_url(get_settings().redis_url, decode_responses=True)


_limiter: LoginRateLimiter | None = None


def get_login_limiter() -> LoginRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = LoginRateLimiter(get_redis())
    return _limiter


def set_login_limiter(limiter: LoginRateLimiter | None) -> None:
    """Tests inject a limiter backed by an in-memory fake Redis."""
    global _limiter
    _limiter = limiter


# --------------------------------------------------------------------------- login


class LoginFailed(ServiceError):
    default_message = "Login yoki parol noto‘g‘ri."


class PasswordChangeRequired(ServiceError):
    """Credentials were correct but the temporary password must be replaced first."""

    default_message = "Birinchi kirishda vaqtinchalik parolni almashtiring."


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str  # plain session token; returned to the Mini App once, never stored
    context: teacher_sessions.TeacherContext


async def login(
    session: AsyncSession,
    *,
    user: User,
    login: str,
    password: str,
    new_password: str | None = None,
    limiter: LoginRateLimiter | None = None,
) -> LoginResult:
    """Authenticate a teacher for the Telegram user `user` (verified via Mini App initData).

    Commits the session itself (audit rows of failed attempts must persist even though the
    caller turns the exception into an error response).

    Raises: LoginLocked, LoginUnavailable, LoginFailed, AccessDenied (blocked / foreign
    Telegram account), PasswordChangeRequired (temporary password, `new_password` missing),
    InvalidInput (new password rejected by policy).
    """
    limiter = limiter or get_login_limiter()
    norm_login = normalize_login(login)[:64]
    subjects = LoginRateLimiter.subjects(norm_login, user.telegram_id)
    await limiter.check(subjects)

    account = None
    if norm_login and len(password or "") <= PASSWORD_MAX_LENGTH:
        # Row lock: concurrent logins of the same account serialize, so "one active session
        # per teacher" holds even under races.
        account = await session.scalar(
            select(TeacherAccount).where(TeacherAccount.login == norm_login).with_for_update()
        )
    # Oversize passwords never reach argon2 with their full length (the web layer caps them too).
    ok = await verify_password(
        account.password_hash if account else _DUMMY_HASH, (password or "")[:PASSWORD_MAX_LENGTH]
    )

    async def fail(reason: str, exc: ServiceError) -> None:
        locked = await limiter.register_failure(subjects)
        await audit.log_action(
            session,
            "teacher.login_failed",
            actor=user,
            entity_type="teacher_account",
            entity_id=account.id if account else None,
            details={"reason": reason, "login": norm_login, "locked": locked},
        )
        await session.commit()
        raise exc

    if account is None or not ok:
        await fail("bad_credentials", LoginFailed())
    assert account is not None

    if account.is_blocked:
        await fail("blocked", AccessDenied("Hisobingiz administrator tomonidan bloklangan."))
    if account.user_id is not None and account.user_id != user.id:
        await fail(
            "foreign_telegram_account",
            AccessDenied(
                "Bu hisob boshqa Telegram akkauntga biriktirilgan. Administratorga murojaat qiling."
            ),
        )
    if account.user_id is None:
        other = await session.scalar(
            select(TeacherAccount.id).where(
                TeacherAccount.user_id == user.id, TeacherAccount.id != account.id
            )
        )
        if other is not None:
            await fail(
                "telegram_account_taken",
                AccessDenied("Bu Telegram akkaunt boshqa o‘qituvchi hisobiga biriktirilgan."),
            )

    if account.must_change_password:
        if not new_password:
            await session.commit()  # release the row lock; nothing changed
            raise PasswordChangeRequired()
        if new_password == password:
            raise InvalidInput("Yangi parol vaqtinchalik paroldan farq qilishi kerak.")
        validate_new_password(new_password, login=account.login)
        account.password_hash = await hash_password(new_password)
        account.must_change_password = False
        account.password_changed_at = utcnow()
        await audit.log_action(
            session,
            "teacher.password_changed",
            actor=user,
            entity_type="teacher_account",
            entity_id=account.id,
            details={"first_login": True},
        )
    elif needs_rehash(account.password_hash):
        account.password_hash = await hash_password(password)

    if account.user_id is None:
        account.user_id = user.id
        await audit.log_action(
            session,
            "teacher.telegram_bound",
            actor=user,
            entity_type="teacher_account",
            entity_id=account.id,
            details={"telegram_id": user.telegram_id},
        )
    _set_teacher_role(user, blocked=False)

    account.last_login_at = utcnow()
    token, ctx = await teacher_sessions.create_session(session, account, user)
    await audit.log_action(
        session,
        "teacher.login",
        actor=user,
        entity_type="teacher_account",
        entity_id=account.id,
        details={"session_id": ctx.session.id, "expires_at": ctx.session.expires_at.isoformat()},
    )
    await session.commit()
    await limiter.reset(subjects)
    return LoginResult(token=token, context=ctx)


async def change_password(
    session: AsyncSession,
    ctx: teacher_sessions.TeacherContext,
    user: User,
    *,
    current_password: str,
    new_password: str,
    limiter: LoginRateLimiter | None = None,
) -> LoginResult:
    """Voluntary password change from the cabinet. Ends every session of the teacher
    (`password_changed`) and returns a fresh one.

    The new session gets a new 24h window. That is a re-authentication (the current password is
    verified), not activity extending a session: the old session is revoked, not prolonged.
    A wrong current password counts as a failed login."""
    limiter = limiter or get_login_limiter()
    account = ctx.teacher
    subjects = LoginRateLimiter.subjects(account.login, user.telegram_id)
    await limiter.check(subjects)
    if not await verify_password(account.password_hash, current_password or ""):
        locked = await limiter.register_failure(subjects)
        await audit.log_action(
            session,
            "teacher.password_change_failed",
            actor=user,
            entity_type="teacher_account",
            entity_id=account.id,
            details={"locked": locked},
        )
        await session.commit()
        raise LoginFailed("Joriy parol noto‘g‘ri.")
    if new_password == current_password:
        raise InvalidInput("Yangi parol joriy paroldan farq qilishi kerak.")
    validate_new_password(new_password, login=account.login)

    account.password_hash = await hash_password(new_password)
    account.password_changed_at = utcnow()
    await teacher_sessions.revoke_all_for_teacher(
        session, account.id, TeacherSessionEndReason.PASSWORD_CHANGED
    )
    token, new_ctx = await teacher_sessions.create_session(session, account, user)
    await audit.log_action(
        session,
        "teacher.password_changed",
        actor=user,
        entity_type="teacher_account",
        entity_id=account.id,
        details={"first_login": False, "session_id": new_ctx.session.id},
    )
    await session.commit()
    await limiter.reset(subjects)
    return LoginResult(token=token, context=new_ctx)


# --------------------------------------------------------------------------- admin operations


def _set_teacher_role(user: User, *, blocked: bool) -> None:
    """Bound + not blocked -> role teacher; unbound/blocked -> student. Admins keep their role."""
    if user.role == UserRole.ADMIN:
        return
    user.role = UserRole.STUDENT if blocked else UserRole.TEACHER


async def get_teacher(session: AsyncSession, teacher_id: int, *, for_update: bool = False) -> TeacherAccount:
    stmt = select(TeacherAccount).where(TeacherAccount.id == teacher_id)
    if for_update:
        stmt = stmt.with_for_update()
    account = await session.scalar(stmt)
    if account is None:
        raise NotFound("O‘qituvchi topilmadi.")
    return account


async def create_teacher(
    session: AsyncSession,
    *,
    actor: User,
    full_name: str,
    login: str,
    telegram_id: int | None = None,
) -> tuple[TeacherAccount, str]:
    """Create a teacher account with a temporary password. Returns (account, temp_password).

    If `telegram_id` is given, that (already registered) bot user is bound right away.
    """
    full_name = " ".join((full_name or "").split())
    if not 2 <= len(full_name) <= 255:
        raise InvalidInput("F.I.Sh. 2–255 belgidan iborat bo‘lishi kerak.")
    login = validate_login(login)
    if await session.scalar(select(TeacherAccount.id).where(TeacherAccount.login == login)) is not None:
        raise Conflict("Bu login band. Boshqa login tanlang.")

    bound_user: User | None = None
    if telegram_id is not None:
        bound_user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if bound_user is None:
            raise NotFound(
                "Bu Telegram ID bilan foydalanuvchi topilmadi. U avval botga /start bosishi kerak."
            )
        taken = await session.scalar(select(TeacherAccount.id).where(TeacherAccount.user_id == bound_user.id))
        if taken is not None:
            raise Conflict("Bu Telegram akkaunt boshqa o‘qituvchi hisobiga biriktirilgan.")

    temp_password = generate_temp_password()
    account = TeacherAccount(
        login=login,
        full_name=full_name,
        password_hash=await hash_password(temp_password),
        must_change_password=True,
        user_id=bound_user.id if bound_user else None,
        created_by_user_id=actor.id,
    )
    session.add(account)
    await session.flush()
    if bound_user is not None:
        _set_teacher_role(bound_user, blocked=False)
    await audit.log_action(
        session,
        "teacher.created",
        actor=actor,
        entity_type="teacher_account",
        entity_id=account.id,
        details={"login": login, "full_name": full_name, "telegram_id": telegram_id},
    )
    return account, temp_password


async def set_blocked(
    session: AsyncSession, *, actor: User, teacher_id: int, blocked: bool
) -> TeacherAccount:
    account = await get_teacher(session, teacher_id, for_update=True)
    if account.is_blocked == blocked:
        return account
    account.is_blocked = blocked
    account.blocked_at = utcnow() if blocked else None
    ended = 0
    if blocked:
        ended = await teacher_sessions.revoke_all_for_teacher(
            session, account.id, TeacherSessionEndReason.ACCOUNT_BLOCKED, revoked_by=actor
        )
    if account.user_id is not None:
        bound_user = await session.get(User, account.user_id)
        if bound_user is not None:
            _set_teacher_role(bound_user, blocked=blocked)
    await audit.log_action(
        session,
        "teacher.blocked" if blocked else "teacher.unblocked",
        actor=actor,
        entity_type="teacher_account",
        entity_id=account.id,
        details={"sessions_ended": ended},
    )
    return account


async def reset_password(
    session: AsyncSession, *, actor: User, teacher_id: int
) -> tuple[TeacherAccount, str]:
    """New temporary password (must be changed on next login); ends all sessions."""
    account = await get_teacher(session, teacher_id, for_update=True)
    temp_password = generate_temp_password()
    account.password_hash = await hash_password(temp_password)
    account.must_change_password = True
    account.password_changed_at = utcnow()
    ended = await teacher_sessions.revoke_all_for_teacher(
        session, account.id, TeacherSessionEndReason.PASSWORD_CHANGED, revoked_by=actor
    )
    await audit.log_action(
        session,
        "teacher.password_reset",
        actor=actor,
        entity_type="teacher_account",
        entity_id=account.id,
        details={"sessions_ended": ended},
    )
    return account, temp_password


async def reset_binding(session: AsyncSession, *, actor: User, teacher_id: int) -> TeacherAccount:
    """Unbind the Telegram account (next successful login binds again); ends all sessions."""
    account = await get_teacher(session, teacher_id, for_update=True)
    old_user_id = account.user_id
    if old_user_id is None:
        return account
    account.user_id = None
    ended = await teacher_sessions.revoke_all_for_teacher(
        session, account.id, TeacherSessionEndReason.ADMIN_REVOKED, revoked_by=actor
    )
    old_user = await session.get(User, old_user_id)
    if old_user is not None:
        _set_teacher_role(old_user, blocked=True)
    await audit.log_action(
        session,
        "teacher.binding_reset",
        actor=actor,
        entity_type="teacher_account",
        entity_id=account.id,
        details={"old_user_id": old_user_id, "sessions_ended": ended},
    )
    return account


ASSIGNMENT_RIGHTS = (
    "can_manage_content",
    "can_manage_quizzes",
    "can_view_students",
    "can_view_results",
    "can_chat",
)


async def assign_course(
    session: AsyncSession, *, actor: User, teacher_id: int, course_id: int
) -> TeacherCourseAssignment:
    await get_teacher(session, teacher_id)
    if await session.get(Course, course_id) is None:
        raise NotFound("Kurs topilmadi.")
    existing = await session.scalar(
        select(TeacherCourseAssignment).where(
            TeacherCourseAssignment.teacher_id == teacher_id, TeacherCourseAssignment.course_id == course_id
        )
    )
    if existing is not None:
        return existing
    assignment = TeacherCourseAssignment(teacher_id=teacher_id, course_id=course_id)
    session.add(assignment)
    await session.flush()
    await audit.log_action(
        session,
        "teacher.course_assigned",
        actor=actor,
        entity_type="teacher_account",
        entity_id=teacher_id,
        details={"course_id": course_id},
    )
    return assignment


async def unassign_course(session: AsyncSession, *, actor: User, teacher_id: int, course_id: int) -> None:
    assignment = await session.scalar(
        select(TeacherCourseAssignment).where(
            TeacherCourseAssignment.teacher_id == teacher_id, TeacherCourseAssignment.course_id == course_id
        )
    )
    if assignment is None:
        return
    await session.delete(assignment)
    await audit.log_action(
        session,
        "teacher.course_unassigned",
        actor=actor,
        entity_type="teacher_account",
        entity_id=teacher_id,
        details={"course_id": course_id},
    )


async def toggle_right(
    session: AsyncSession, *, actor: User, teacher_id: int, course_id: int, right: str
) -> TeacherCourseAssignment:
    if right not in ASSIGNMENT_RIGHTS:
        raise InvalidInput()
    assignment = await session.scalar(
        select(TeacherCourseAssignment)
        .where(
            TeacherCourseAssignment.teacher_id == teacher_id, TeacherCourseAssignment.course_id == course_id
        )
        .with_for_update()
    )
    if assignment is None:
        raise NotFound("Biriktirish topilmadi.")
    value = not getattr(assignment, right)
    setattr(assignment, right, value)
    await audit.log_action(
        session,
        "teacher.course_right_changed",
        actor=actor,
        entity_type="teacher_account",
        entity_id=teacher_id,
        details={"course_id": course_id, "right": right, "value": value},
    )
    return assignment
