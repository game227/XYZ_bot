"""Test helpers for AUTH (teacher accounts, sessions, chat). Not a test module."""

import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import Course, Subject, TeacherAccount, TeacherCourseAssignment, User
from app.services import auth


def use_cheap_hasher(monkeypatch) -> None:
    """Argon2id with tiny params for tests (production: 64 MiB per hash)."""
    from argon2 import PasswordHasher

    cheap = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    monkeypatch.setattr(auth, "_hasher", cheap)
    monkeypatch.setattr(auth, "_DUMMY_HASH", cheap.hash("dummy-password-for-timing"))


class FakeRedis:
    """In-memory subset of redis.asyncio used by the login limiter (TTL via a fake clock)."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[object, float | None]] = {}
        self.now = 1_000_000.0
        self.fail = False

    def _alive(self, key: str) -> bool:
        item = self.data.get(key)
        if item is None:
            return False
        if item[1] is not None and item[1] <= self.now:
            del self.data[key]
            return False
        return True

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis down")

    async def get(self, name: str) -> object:
        self._check()
        return self.data[name][0] if self._alive(name) else None

    async def set(self, name: str, value: object, ex: int | None = None, nx: bool = False) -> object:
        self._check()
        if nx and self._alive(name):
            return None
        self.data[name] = (value, self.now + ex if ex else None)
        return True

    async def incr(self, name: str) -> int:
        self._check()
        value, exp = self.data[name] if self._alive(name) else (0, None)
        value = int(value) + 1  # type: ignore[call-overload]
        self.data[name] = (value, exp)
        return value

    async def delete(self, *names: str) -> int:
        self._check()
        return sum(1 for n in names if self.data.pop(n, None) is not None)

    async def ttl(self, name: str) -> int:
        self._check()
        if not self._alive(name):
            return -2
        exp = self.data[name][1]
        return -1 if exp is None else int(exp - self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_limiter() -> tuple[auth.LoginRateLimiter, FakeRedis]:
    redis = FakeRedis()
    return auth.LoginRateLimiter(redis), redis


def sign_init_data(telegram_id: int, *, auth_date: int | None = None, first_name: str = "Test") -> str:
    """Valid Telegram Mini App initData for the test bot token."""
    token = get_settings().bot_token.get_secret_value()
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH-test",
        "user": json.dumps({"id": telegram_id, "first_name": first_name}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


TEACHER_PASSWORD = "Strong-pass-2026"


async def make_teacher(
    session: AsyncSession,
    *,
    login: str = "teacher1",
    password: str = TEACHER_PASSWORD,
    user: User | None = None,
    must_change_password: bool = False,
    is_blocked: bool = False,
    full_name: str = "Test O‘qituvchi",
) -> TeacherAccount:
    account = TeacherAccount(
        login=login,
        full_name=full_name,
        password_hash=await auth.hash_password(password),
        must_change_password=must_change_password,
        is_blocked=is_blocked,
        user_id=user.id if user else None,
    )
    session.add(account)
    await session.flush()
    return account


async def make_course(session: AsyncSession, title: str = "Matematika") -> Course:
    subject = Subject(title="Fan " + title)
    session.add(subject)
    await session.flush()
    course = Course(subject_id=subject.id, title=title)
    session.add(course)
    await session.flush()
    return course


async def assign(session: AsyncSession, teacher: TeacherAccount, course: Course, **rights: bool) -> None:
    session.add(TeacherCourseAssignment(teacher_id=teacher.id, course_id=course.id, **rights))
    await session.flush()


class NullBot:
    """Stands in for aiogram.Bot in web side effects; records sent messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.sent.append((chat_id, text))

    async def __aenter__(self) -> "NullBot":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@asynccontextmanager
async def null_bot_ctx():
    yield NullBot()
