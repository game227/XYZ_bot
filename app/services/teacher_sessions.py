"""Strict 24h teacher sessions (TZ §7.3–7.5, §7.6). OWNER: AUTH.

Rules enforced here, on every call, from the database (nothing is cached):
- A session is valid iff `revoked_at IS NULL AND now < expires_at` and the teacher account
  is not blocked and is still bound to the same Telegram user.
- `expires_at = created_at + TEACHER_SESSION_TTL_HOURS`, set once at login and never updated
  (activity does not extend it).
- One active session per teacher: creating a session ends the older ones (`replaced`).
- Each session gets an unguessable id; only its sha256 is stored.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import (
    TeacherAccount,
    TeacherCourseAssignment,
    TeacherSession,
    TeacherSessionEndReason,
    User,
)
from app.services import audit
from app.services.errors import AccessDenied, TeacherSessionRequired
from app.utils.time import utcnow

TOKEN_BYTES = 32

COURSE_RIGHTS = frozenset(
    {"can_manage_content", "can_manage_quizzes", "can_view_students", "can_view_results", "can_chat"}
)


@dataclass(frozen=True, slots=True)
class TeacherContext:
    teacher: TeacherAccount
    session: TeacherSession


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_hint(token: str | None) -> str:
    """Safe-to-log fragment of a token (TZ §7.6: tokens never appear in full in logs)."""
    return f"{token[:4]}…" if token else "-"


def session_ttl() -> timedelta:
    return timedelta(hours=get_settings().teacher_session_ttl_hours)


def _active_stmt(now: datetime):
    return (
        select(TeacherSession, TeacherAccount)
        .join(TeacherAccount, TeacherAccount.id == TeacherSession.teacher_id)
        .where(
            TeacherSession.revoked_at.is_(None),
            TeacherSession.expires_at > now,
            TeacherAccount.is_blocked.is_(False),
            TeacherAccount.must_change_password.is_(False),
            TeacherAccount.user_id.is_not(None),
        )
    )


async def get_active_session(
    session: AsyncSession, telegram_id: int, *, now: datetime | None = None
) -> TeacherSession | None:
    """The valid session bound to this Telegram ID, or None. Never extends anything."""
    ctx = await get_active_context(session, telegram_id, now=now)
    return ctx.session if ctx else None


async def get_active_context(
    session: AsyncSession, telegram_id: int, *, now: datetime | None = None
) -> TeacherContext | None:
    now = now or utcnow()
    stmt = (
        _active_stmt(now)
        .join(User, User.id == TeacherAccount.user_id)
        .where(TeacherSession.telegram_id == telegram_id, User.telegram_id == telegram_id)
        .order_by(TeacherSession.created_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return TeacherContext(teacher=row[1], session=row[0]) if row else None


async def require_teacher(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> TeacherContext:
    """Every teacher handler calls this first. Raises TeacherSessionRequired."""
    ctx = await get_active_context(session, user.telegram_id, now=now)
    if ctx is None or ctx.teacher.user_id != user.id:
        raise TeacherSessionRequired()
    return ctx


async def create_session(
    session: AsyncSession, account: TeacherAccount, user: User, *, now: datetime | None = None
) -> tuple[str, TeacherContext]:
    """End the teacher's other sessions (`replaced`) and open a new strict-24h one.

    Callers must hold the account row lock (auth.login does) so concurrent logins serialize.
    Returns (plain_token, context); the plain token is never persisted.
    """
    now = now or utcnow()
    replaced = await revoke_all_for_teacher(session, account.id, TeacherSessionEndReason.REPLACED, now=now)
    token = new_token()
    ts = TeacherSession(
        teacher_id=account.id,
        telegram_id=user.telegram_id,
        token_hash=hash_token(token),
        created_at=now,
        expires_at=now + session_ttl(),
    )
    session.add(ts)
    await session.flush()
    await audit.log_action(
        session,
        "teacher_session.created",
        actor=user,
        entity_type="teacher_session",
        entity_id=ts.id,
        details={"teacher_id": account.id, "replaced": replaced, "expires_at": ts.expires_at.isoformat()},
    )
    return token, TeacherContext(teacher=account, session=ts)


async def revoke_all_for_teacher(
    session: AsyncSession,
    teacher_id: int,
    reason: TeacherSessionEndReason,
    *,
    revoked_by: User | None = None,
    now: datetime | None = None,
) -> int:
    """End every not-yet-revoked session of the teacher (expired ones included). Returns how many
    were still active."""
    now = now or utcnow()
    result = await session.execute(
        update(TeacherSession)
        .where(TeacherSession.teacher_id == teacher_id, TeacherSession.revoked_at.is_(None))
        .values(revoked_at=now, end_reason=reason, revoked_by_user_id=revoked_by.id if revoked_by else None)
        .returning(TeacherSession.id, TeacherSession.expires_at)
        .execution_options(synchronize_session="fetch")
    )
    rows = result.all()
    if rows:
        await audit.log_action(
            session,
            "teacher_session.ended",
            actor=revoked_by,
            entity_type="teacher_account",
            entity_id=teacher_id,
            details={"reason": str(reason), "session_ids": [r[0] for r in rows]},
        )
    return sum(1 for _, expires_at in rows if expires_at > now)


async def logout(session: AsyncSession, user: User, *, now: datetime | None = None) -> bool:
    """`/logout` and the "🔒 Chiqish" button. Returns False if there was no active session."""
    ctx = await get_active_context(session, user.telegram_id, now=now)
    if ctx is None:
        return False
    now = now or utcnow()
    ended = await session.execute(
        update(TeacherSession)
        .where(TeacherSession.id == ctx.session.id, TeacherSession.revoked_at.is_(None))
        .values(revoked_at=now, end_reason=TeacherSessionEndReason.LOGOUT)
        .returning(TeacherSession.id)
        .execution_options(synchronize_session="fetch")
    )
    if ended.first() is None:
        return False
    await audit.log_action(
        session,
        "teacher_session.logout",
        actor=user,
        entity_type="teacher_session",
        entity_id=ctx.session.id,
        details={"teacher_id": ctx.teacher.id},
    )
    return True


async def revoke_session(
    session: AsyncSession, session_id: int, *, actor: User, now: datetime | None = None
) -> TeacherSession | None:
    """Admin revocation (TZ §7.3): effective on the very next request. Returns the session if
    it was active, None if it was already ended or unknown."""
    now = now or utcnow()
    result = await session.execute(
        update(TeacherSession)
        .where(
            TeacherSession.id == session_id,
            TeacherSession.revoked_at.is_(None),
            TeacherSession.expires_at > now,
        )
        .values(revoked_at=now, end_reason=TeacherSessionEndReason.ADMIN_REVOKED, revoked_by_user_id=actor.id)
        .returning(TeacherSession)
        .execution_options(synchronize_session="fetch")
    )
    ts = result.scalar_one_or_none()
    if ts is not None:
        await audit.log_action(
            session,
            "teacher_session.revoked",
            actor=actor,
            entity_type="teacher_session",
            entity_id=ts.id,
            details={"teacher_id": ts.teacher_id, "telegram_id": ts.telegram_id},
        )
    return ts


async def list_active_sessions(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50, offset: int = 0
) -> list[tuple[TeacherSession, TeacherAccount]]:
    now = now or utcnow()
    stmt = (
        select(TeacherSession, TeacherAccount)
        .join(TeacherAccount, TeacherAccount.id == TeacherSession.teacher_id)
        .where(TeacherSession.revoked_at.is_(None), TeacherSession.expires_at > now)
        .order_by(TeacherSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def teacher_course_ids(session: AsyncSession, teacher_id: int, right: str | None = None) -> set[int]:
    """Course ids assigned to the teacher (with the given per-course right, if any)."""
    stmt = select(TeacherCourseAssignment.course_id).where(TeacherCourseAssignment.teacher_id == teacher_id)
    if right is not None:
        if right not in COURSE_RIGHTS:
            raise ValueError(f"unknown course right: {right}")
        stmt = stmt.where(getattr(TeacherCourseAssignment, right).is_(True))
    return set((await session.scalars(stmt)).all())


async def require_course_right(
    session: AsyncSession, ctx: TeacherContext, course_id: int, right: str
) -> None:
    if course_id not in await teacher_course_ids(session, ctx.teacher.id, right):
        raise AccessDenied("Bu kurs bo‘yicha sizda bunday ruxsat yo‘q.")
