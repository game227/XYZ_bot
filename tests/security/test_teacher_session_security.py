"""Security tests for teacher auth, strict 24h sessions and chat (TZ §7, §15, §22.5–22.10).

Need PostgreSQL (TEST_DATABASE_URL); skipped otherwise. Telegram is never called: web side
effects use NullBot, bot handlers get AsyncMock events.
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AdminAuditLog,
    CourseEnrollment,
    TeacherChatMessage,
    TeacherCourseAssignment,
    TeacherSession,
    TeacherSessionEndReason,
    UserRole,
)
from app.handlers.teacher import cabinet
from app.keyboards.auth import AuCabinetCb
from app.services import auth, chat, teacher_sessions
from app.services.errors import AccessDenied, TeacherSessionRequired
from app.utils.time import utcnow
from tests.teacher_helpers import (
    TEACHER_PASSWORD,
    NullBot,
    assign,
    make_course,
    make_limiter,
    make_teacher,
    sign_init_data,
    use_cheap_hasher,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def cheap_hasher(monkeypatch):
    use_cheap_hasher(monkeypatch)


@pytest.fixture(autouse=True)
def limiter():
    lim, redis = make_limiter()
    auth.set_login_limiter(lim)
    yield redis
    auth.set_login_limiter(None)


@pytest.fixture
def clock(monkeypatch):
    """Moves "now" for the session checks (time travel instead of sleeping 24h)."""

    class Clock:
        offset = timedelta(0)

        def now(self):
            return utcnow() + self.offset

        def travel(self, delta: timedelta) -> None:
            self.offset += delta

    c = Clock()
    monkeypatch.setattr(teacher_sessions, "utcnow", c.now)
    return c


async def _login(session: AsyncSession, user, login: str = "teacher1", password: str = TEACHER_PASSWORD):
    return await auth.login(session, user=user, login=login, password=password)


# --------------------------------------------------------------------------- login + strict 24h


async def test_login_creates_strict_24h_session_bound_to_telegram(session, make_user) -> None:
    user = await make_user()
    account = await make_teacher(session)
    result = await _login(session, user, login="  TEACHER1 ")
    ts = result.context.session
    assert ts.expires_at - ts.created_at == timedelta(hours=24)
    assert ts.telegram_id == user.telegram_id
    assert ts.token_hash == teacher_sessions.hash_token(result.token)
    stored = await session.scalar(select(TeacherSession).where(TeacherSession.id == ts.id))
    assert result.token not in (stored.token_hash or "")
    assert account.user_id == user.id
    assert user.role == UserRole.TEACHER


async def test_session_expires_after_24h_and_activity_does_not_extend(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    result = await _login(session, user)
    created = result.context.session.created_at
    expires = result.context.session.expires_at

    # Heavy activity during the day never moves expires_at.
    for hours in (1, 5, 12, 23):
        ctx = await teacher_sessions.require_teacher(session, user, now=created + timedelta(hours=hours))
        await teacher_sessions.rotate_token(session, ctx)
    await session.refresh(result.context.session)
    assert result.context.session.expires_at == expires

    assert await teacher_sessions.get_active_session(
        session, user.telegram_id, now=created + timedelta(hours=23, minutes=59)
    )
    assert await teacher_sessions.get_active_session(session, user.telegram_id, now=expires) is None
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher(session, user, now=expires + timedelta(seconds=1))


async def test_admin_revoke_is_immediate(session, make_user) -> None:
    user = await make_user()
    admin = await make_user(role=UserRole.ADMIN)
    await make_teacher(session)
    result = await _login(session, user)
    revoked = await teacher_sessions.revoke_session(session, result.context.session.id, actor=admin)
    assert revoked is not None and revoked.end_reason == TeacherSessionEndReason.ADMIN_REVOKED
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher(session, user)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher_token(session, result.token, user)
    assert await teacher_sessions.revoke_session(session, result.context.session.id, actor=admin) is None
    actions = (await session.scalars(select(AdminAuditLog.action))).all()
    assert "teacher_session.revoked" in actions


async def test_logout_ends_session(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    await _login(session, user)
    assert await teacher_sessions.logout(session, user)
    assert not await teacher_sessions.logout(session, user)
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None
    ts = await session.scalar(select(TeacherSession).where(TeacherSession.telegram_id == user.telegram_id))
    assert ts.end_reason == TeacherSessionEndReason.LOGOUT


async def test_new_login_replaces_old_session(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    first = await _login(session, user)
    second = await _login(session, user)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher_token(session, first.token, user)
    assert (await teacher_sessions.require_teacher_token(session, second.token, user)).session.id == (
        second.context.session.id
    )
    await session.refresh(first.context.session)
    assert first.context.session.end_reason == TeacherSessionEndReason.REPLACED
    active = await session.scalar(
        select(func.count(TeacherSession.id)).where(
            TeacherSession.revoked_at.is_(None), TeacherSession.expires_at > utcnow()
        )
    )
    assert active == 1


async def test_session_bound_to_telegram_user(session, make_user) -> None:
    user = await make_user()
    other = await make_user()
    await make_teacher(session)
    result = await _login(session, user)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher(session, other)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher_token(session, result.token, other)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher_token(session, None, user)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher_token(session, result.token + "x", user)


# --------------------------------------------------------------------------- login failures


async def test_wrong_password_and_unknown_login_look_the_same(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    with pytest.raises(auth.LoginFailed) as e1:
        await _login(session, user, password="Wrong-pass-0000")
    with pytest.raises(auth.LoginFailed) as e2:
        await _login(session, user, login="nobody")
    assert e1.value.user_message == e2.value.user_message
    rows = (
        await session.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "teacher.login_failed"))
    ).all()
    assert len(rows) == 2
    dump = repr([r.details for r in rows])
    assert "Wrong-pass-0000" not in dump and TEACHER_PASSWORD not in dump


async def test_bruteforce_locks_login_even_with_correct_password(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    for _ in range(auth.LOGIN_MAX_FAILURES):
        with pytest.raises(auth.LoginFailed):
            await _login(session, user, password="Wrong-pass-0000")
    with pytest.raises(auth.LoginLocked):
        await _login(session, user)


async def test_blocked_account_cannot_login_and_loses_session(session, make_user) -> None:
    user = await make_user()
    admin = await make_user(role=UserRole.ADMIN)
    account = await make_teacher(session)
    await _login(session, user)
    await auth.set_blocked(session, actor=admin, teacher_id=account.id, blocked=True)
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None
    assert user.role == UserRole.STUDENT
    with pytest.raises(AccessDenied):
        await _login(session, user)


async def test_login_from_foreign_telegram_account_rejected(session, make_user) -> None:
    owner = await make_user()
    intruder = await make_user()
    await make_teacher(session, user=owner)
    with pytest.raises(AccessDenied):
        await _login(session, intruder)
    assert await teacher_sessions.get_active_session(session, intruder.telegram_id) is None


async def test_first_login_requires_password_change(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session, password="Temp-pass-111", must_change_password=True)
    with pytest.raises(auth.PasswordChangeRequired):
        await _login(session, user, password="Temp-pass-111")
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None
    result = await auth.login(
        session, user=user, login="teacher1", password="Temp-pass-111", new_password="Brand-new-pass-7"
    )
    assert result.context.teacher.must_change_password is False
    with pytest.raises(auth.LoginFailed):
        await _login(session, user, password="Temp-pass-111")


async def test_change_password_reauthenticates_with_fresh_session(session, make_user) -> None:
    """A password change verifies the current password and starts a NEW session (old one revoked);
    this is re-authentication, not activity extending the old session."""
    user = await make_user()
    await make_teacher(session)
    old = await _login(session, user)
    with pytest.raises(auth.LoginFailed):
        await auth.change_password(
            session, old.context, user, current_password="bad", new_password="Next-pass-2027"
        )
    new = await auth.change_password(
        session, old.context, user, current_password=TEACHER_PASSWORD, new_password="Next-pass-2027"
    )
    await session.refresh(old.context.session)
    assert old.context.session.end_reason == TeacherSessionEndReason.PASSWORD_CHANGED
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher_token(session, old.token, user)
    assert new.context.session.id != old.context.session.id


async def test_reset_binding_ends_sessions(session, make_user) -> None:
    user = await make_user()
    admin = await make_user(role=UserRole.ADMIN)
    account = await make_teacher(session)
    await _login(session, user)
    await auth.reset_binding(session, actor=admin, teacher_id=account.id)
    assert account.user_id is None
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None


# --------------------------------------------------------------------------- web (Mini App)


@pytest_asyncio.fixture
async def client(session: AsyncSession, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    from app.web.app import app
    from app.web.deps import get_db
    from app.web.routers import teacher as teacher_router

    async def _db() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr(teacher_router, "_bot", NullBot)
    app.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


def _headers(user, token: str | None = None) -> dict[str, str]:
    h = {"X-Telegram-Init-Data": sign_init_data(user.telegram_id)}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _chat_setup(session, make_user):
    teacher_user = await make_user()
    student = await make_user(first_name="Talaba")
    # Bound like after a first login: students can only write to bound teachers.
    account = await make_teacher(session, user=teacher_user)
    course = await make_course(session)
    await assign(session, account, course, can_chat=True)
    session.add(CourseEnrollment(user_id=student.id, course_id=course.id))
    await session.flush()
    thread = await chat.get_or_create_thread(
        session, teacher_id=account.id, student_user_id=student.id, course_id=course.id
    )
    await chat.student_send(session, student, thread, "Maxfiy savol: uy vazifasi")
    return teacher_user, student, account, course, thread


async def _web_login(client, user) -> str:
    resp = await client.post(
        "/teacher/api/login", json={"login": "teacher1", "password": TEACHER_PASSWORD}, headers=_headers(user)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def test_web_chat_closed_after_24h_history_kept(session, make_user, client, clock) -> None:
    teacher_user, _, _, _, thread = await _chat_setup(session, make_user)
    token = await _web_login(client, teacher_user)

    ok = await client.get("/teacher/api/threads", headers=_headers(teacher_user, token))
    assert ok.status_code == 200 and ok.json()["threads"][0]["id"] == thread.id
    msgs = await client.get(
        f"/teacher/api/threads/{thread.id}/messages", headers=_headers(teacher_user, token)
    )
    assert "Maxfiy savol" in msgs.text

    clock.travel(timedelta(hours=24, seconds=1))
    for method, path, body in [
        ("GET", "/teacher/api/me", None),
        ("GET", "/teacher/api/threads", None),
        ("GET", f"/teacher/api/threads/{thread.id}/messages", None),
        ("POST", f"/teacher/api/threads/{thread.id}/messages", {"body": "salom"}),
        (
            "POST",
            "/teacher/api/password",
            {"current_password": TEACHER_PASSWORD, "new_password": "Xyz-pass-2027"},
        ),
        ("POST", "/teacher/api/resume", None),
    ]:
        resp = await client.request(method, path, json=body, headers=_headers(teacher_user, token))
        assert resp.status_code == 401, (path, resp.text)
        assert "Maxfiy" not in resp.text
        if path != "/teacher/api/resume":
            assert resp.json()["detail"]["code"] == "session"
    # Session end does not delete chat history.
    assert await session.scalar(select(func.count(TeacherChatMessage.id))) == 1


async def test_web_admin_revoke_blocks_next_request(session, make_user, client) -> None:
    teacher_user, *_ = await _chat_setup(session, make_user)
    admin = await make_user(role=UserRole.ADMIN)
    token = await _web_login(client, teacher_user)
    assert (
        await client.get("/teacher/api/threads", headers=_headers(teacher_user, token))
    ).status_code == 200
    active = await teacher_sessions.get_active_session(session, teacher_user.telegram_id)
    await teacher_sessions.revoke_session(session, active.id, actor=admin)
    assert (
        await client.get("/teacher/api/threads", headers=_headers(teacher_user, token))
    ).status_code == 401


async def test_web_requires_token_and_matching_telegram_user(session, make_user, client) -> None:
    teacher_user, student, *_ = await _chat_setup(session, make_user)
    token = await _web_login(client, teacher_user)
    assert (await client.get("/teacher/api/threads", headers=_headers(teacher_user))).status_code == 401
    # Stolen token used from another Telegram account.
    assert (await client.get("/teacher/api/threads", headers=_headers(student, token))).status_code == 401
    # Valid initData alone is not enough; a bad initData signature is rejected outright.
    bad = {
        "X-Telegram-Init-Data": sign_init_data(teacher_user.telegram_id) + "0",
        "Authorization": f"Bearer {token}",
    }
    assert (await client.get("/teacher/api/threads", headers=bad)).status_code == 401


async def test_web_resume_rotates_token_without_extending(session, make_user, client) -> None:
    teacher_user, *_ = await _chat_setup(session, make_user)
    token = await _web_login(client, teacher_user)
    expires = (await teacher_sessions.get_active_session(session, teacher_user.telegram_id)).expires_at
    resp = await client.post("/teacher/api/resume", headers=_headers(teacher_user))
    assert resp.status_code == 200
    new_token = resp.json()["token"]
    assert new_token != token
    assert (await client.get("/teacher/api/me", headers=_headers(teacher_user, token))).status_code == 401
    assert (await client.get("/teacher/api/me", headers=_headers(teacher_user, new_token))).status_code == 200
    assert (
        await teacher_sessions.get_active_session(session, teacher_user.telegram_id)
    ).expires_at == expires


async def test_web_logout(session, make_user, client) -> None:
    teacher_user, *_ = await _chat_setup(session, make_user)
    token = await _web_login(client, teacher_user)
    assert (
        await client.post("/teacher/api/logout", headers=_headers(teacher_user, token))
    ).status_code == 200
    assert (
        await client.get("/teacher/api/threads", headers=_headers(teacher_user, token))
    ).status_code == 401


async def test_web_teacher_sees_only_own_threads_with_chat_right(session, make_user, client) -> None:
    teacher_user, _, account, course, thread = await _chat_setup(session, make_user)
    other_user = await make_user()
    other = await make_teacher(session, login="teacher2", user=other_user)
    await assign(session, other, course, can_chat=True)
    resp = await client.post(
        "/teacher/api/login",
        json={"login": "teacher2", "password": TEACHER_PASSWORD},
        headers=_headers(other_user),
    )
    other_token = resp.json()["token"]
    assert (await client.get("/teacher/api/threads", headers=_headers(other_user, other_token))).json()[
        "threads"
    ] == []
    resp = await client.get(
        f"/teacher/api/threads/{thread.id}/messages", headers=_headers(other_user, other_token)
    )
    assert resp.status_code == 404 and "Maxfiy" not in resp.text

    token = await _web_login(client, teacher_user)
    assignment = await session.scalar(
        select(TeacherCourseAssignment).where(TeacherCourseAssignment.teacher_id == account.id)
    )
    assignment.can_chat = False
    await session.flush()
    assert (await client.get("/teacher/api/threads", headers=_headers(teacher_user, token))).json()[
        "threads"
    ] == []
    resp = await client.get(
        f"/teacher/api/threads/{thread.id}/messages", headers=_headers(teacher_user, token)
    )
    assert resp.status_code == 403 and "Maxfiy" not in resp.text


async def test_web_teacher_reply_delivered_to_student(session, make_user, client, monkeypatch) -> None:
    teacher_user, student, _, _, thread = await _chat_setup(session, make_user)
    token = await _web_login(client, teacher_user)
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(chat, "deliver_to_student", delivered)
    resp = await client.post(
        f"/teacher/api/threads/{thread.id}/messages",
        json={"body": "Javob"},
        headers=_headers(teacher_user, token),
    )
    assert resp.status_code == 200 and resp.json()["delivered"] is True
    assert delivered.await_args.args[1].id == student.id


# --------------------------------------------------------------------------- bot handlers


def _message() -> AsyncMock:
    return AsyncMock()


def _callback() -> AsyncMock:
    cb = AsyncMock()
    cb.message = AsyncMock()
    return cb


async def test_bot_teacher_handlers_closed_after_expiry(session, make_user, clock) -> None:
    teacher_user, student, account, course, _ = await _chat_setup(session, make_user)
    await assign_rights(session, account.id, can_view_students=True, can_view_results=True)
    await _login(session, teacher_user)

    # With a valid session the cabinet shows data.
    msg = _message()
    await cabinet.my_courses(msg, session, teacher_user)
    assert course.title in msg.answer.await_args.args[0]

    clock.travel(timedelta(hours=24))
    message_handlers = [
        cabinet.my_courses,
        cabinet.chats_entry,
        cabinet.students_entry,
        cabinet.results_entry,
    ]
    for handler in message_handlers:
        msg = _message()
        await handler(msg, session, teacher_user)
        sent = " ".join(str(c.args[0]) for c in msg.answer.await_args_list)
        assert "qayta" in sent.lower(), handler.__name__
        assert course.title not in sent and "Talaba" not in sent

    callback_cases = [
        (cabinet.students_list, AuCabinetCb(action="students", course_id=course.id)),
        (cabinet.student_detail, AuCabinetCb(action="student", course_id=course.id, user_id=student.id)),
        (cabinet.results_list, AuCabinetCb(action="results", course_id=course.id)),
    ]
    for handler, data in callback_cases:
        cb = _callback()
        await handler(cb, data, session, teacher_user)
        cb.message.edit_text.assert_not_awaited()
        sent = " ".join(str(c.args[0]) for c in cb.answer.await_args_list + cb.message.answer.await_args_list)
        assert "Talaba" not in sent and course.title not in sent


async def test_bot_handlers_enforce_course_rights(session, make_user) -> None:
    teacher_user, student, _, course, _ = await _chat_setup(session, make_user)  # can_view_* default false
    await _login(session, teacher_user)
    with pytest.raises(AccessDenied):
        await cabinet.students_list(
            _callback(), AuCabinetCb(action="students", course_id=course.id), session, teacher_user
        )
    with pytest.raises(AccessDenied):
        await cabinet.student_detail(
            _callback(),
            AuCabinetCb(action="student", course_id=course.id, user_id=student.id),
            session,
            teacher_user,
        )


async def test_bot_logout_command(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    await _login(session, user)
    msg = _message()
    await cabinet.cmd_logout(msg, session, user)
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None


async def assign_rights(session: AsyncSession, teacher_id: int, **rights: bool) -> None:
    assignment = await session.scalar(
        select(TeacherCourseAssignment).where(TeacherCourseAssignment.teacher_id == teacher_id)
    )
    for k, v in rights.items():
        setattr(assignment, k, v)
    await session.flush()


async def test_student_message_notice_has_no_text(monkeypatch) -> None:
    from tests.teacher_helpers import FakeRedis

    monkeypatch.setattr(auth, "get_redis", lambda: FakeRedis())
    bot = NullBot()
    await chat.notify_teacher(bot, 42, teacher_id=999_001)
    assert bot.sent and "Maxfiy" not in bot.sent[0][1]
    assert bot.sent[0][1] == chat.NEW_MESSAGE_NOTICE
