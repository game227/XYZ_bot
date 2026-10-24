"""Security tests for teacher auth, strict 24h sessions and chat (TZ §7, §15, §22.5–22.10).

Need PostgreSQL (TEST_DATABASE_URL); skipped otherwise. Telegram is never called: web side
effects use NullBot, bot handlers get AsyncMock events.
"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
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
from app.handlers.teacher import chats as teacher_chats
from app.keyboards.auth import AuCabinetCb
from app.services import auth, chat, teacher_sessions, teacher_trail
from app.services.errors import AccessDenied, NotFound, TeacherSessionRequired
from app.utils.time import utcnow
from tests.teacher_helpers import (
    TEACHER_PASSWORD,
    NullBot,
    assign,
    make_course,
    make_limiter,
    make_teacher,
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
        await teacher_sessions.require_teacher(session, user, now=created + timedelta(hours=hours))
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
    assert (await teacher_sessions.require_teacher(session, user)).session.id == second.context.session.id
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
    await _login(session, user)
    with pytest.raises(TeacherSessionRequired):
        await teacher_sessions.require_teacher(session, other)


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
    assert new.context.session.id != old.context.session.id
    assert (await teacher_sessions.require_teacher(session, user)).session.id == new.context.session.id


async def test_reset_binding_ends_sessions(session, make_user) -> None:
    user = await make_user()
    admin = await make_user(role=UserRole.ADMIN)
    account = await make_teacher(session)
    await _login(session, user)
    await auth.reset_binding(session, actor=admin, teacher_id=account.id)
    assert account.user_id is None
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None


# --------------------------------------------------------------------------- web (Mini App)


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


def _state(user) -> FSMContext:
    return FSMContext(
        MemoryStorage(), StorageKey(bot_id=1, chat_id=user.telegram_id, user_id=user.telegram_id)
    )


def _text_message(text: str = "") -> AsyncMock:
    msg = AsyncMock()
    msg.text = text
    return msg


def _open(thread_id: int) -> teacher_chats.AuThreadCb:
    return teacher_chats.AuThreadCb(action="open", thread_id=thread_id)


def _sent(mock: AsyncMock) -> str:
    calls = mock.answer.await_args_list + getattr(mock.message, "answer", AsyncMock()).await_args_list
    edits = getattr(mock.message, "edit_text", AsyncMock()).await_args_list
    return " ".join(str(c.args[0]) for c in calls + edits if c.args)


# --------------------------------------------------------------------------- bot login (password in chat)


async def test_bot_login_deletes_password_and_opens_session(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    state = _state(user)
    await state.set_state(cabinet.AuLogin.login)
    await cabinet.login_step(_text_message("teacher1"), state)
    pw = _text_message(TEACHER_PASSWORD)
    await cabinet.password_step(pw, session, user, state)
    pw.delete.assert_awaited()  # the password never stays in the chat
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is not None
    assert await state.get_state() is None


async def test_bot_wrong_password_deleted_and_no_session(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session)
    state = _state(user)
    await state.update_data(au_login="teacher1")
    await state.set_state(cabinet.AuLogin.password)
    pw = _text_message("Wrong-password-123")
    await cabinet.password_step(pw, session, user, state)
    pw.delete.assert_awaited()
    assert "noto‘g‘ri" in _sent(pw)
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None
    assert await state.get_state() == cabinet.AuLogin.login.state


async def test_bot_first_login_changes_temporary_password(session, make_user) -> None:
    user = await make_user()
    await make_teacher(session, must_change_password=True)
    state = _state(user)
    await state.update_data(au_login="teacher1")
    await state.set_state(cabinet.AuLogin.password)
    await cabinet.password_step(_text_message(TEACHER_PASSWORD), session, user, state)
    assert await state.get_state() == cabinet.AuLogin.new_password.state
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None
    new_pw = _text_message("Yangi-parol-2026")
    await cabinet.new_password_step(new_pw, session, user, state)
    new_pw.delete.assert_awaited()
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is not None


async def test_bot_password_change_requires_session_and_current_password(session, make_user, clock) -> None:
    user = await make_user()
    await make_teacher(session)
    await _login(session, user)
    state = _state(user)
    await state.set_state(cabinet.AuPassword.current)
    current = _text_message(TEACHER_PASSWORD)
    await cabinet.password_change_current(current, session, user, state)
    current.delete.assert_awaited()
    new_pw = _text_message("Boshqa-parol-2026")
    await cabinet.password_change_new(new_pw, session, user, state)
    new_pw.delete.assert_awaited()
    assert "almashtirildi" in _sent(new_pw)
    # After 24h even the password screen asks for a new login first.
    clock.travel(timedelta(hours=24, seconds=1))
    msg = _text_message()
    await cabinet.password_change_start(msg, session, user, _state(user))
    assert "qayta" in _sent(msg).lower()


# --------------------------------------------------------------------------- bot chats


async def test_bot_chat_closed_after_24h_history_kept(session, make_user, clock) -> None:
    teacher_user, _, _, _, thread = await _chat_setup(session, make_user)
    await _login(session, teacher_user)

    msg = _text_message()
    await teacher_chats.chats_entry(msg, session, teacher_user)
    assert "Talaba" in str(msg.answer.await_args.kwargs.get("reply_markup"))
    cb = _callback()
    await teacher_chats.open_thread(
        cb, teacher_chats.AuThreadCb(action="open", thread_id=thread.id), session, teacher_user
    )
    assert "Maxfiy savol" in _sent(cb)

    clock.travel(timedelta(hours=24))
    msg = _text_message()
    await teacher_chats.chats_entry(msg, session, teacher_user)
    assert "qayta" in _sent(msg).lower() and "Talaba" not in _sent(msg)
    cb = _callback()
    await teacher_chats.open_thread(
        cb, teacher_chats.AuThreadCb(action="open", thread_id=thread.id), session, teacher_user
    )
    cb.message.edit_text.assert_not_awaited()
    assert "Maxfiy" not in _sent(cb)
    # Session end never deletes the history.
    assert await session.scalar(select(func.count(TeacherChatMessage.id))) == 1


async def test_bot_admin_revoke_blocks_next_chat_request(session, make_user) -> None:
    teacher_user, _, _, _, thread = await _chat_setup(session, make_user)
    admin = await make_user(role=UserRole.ADMIN)
    result = await _login(session, teacher_user)
    await teacher_sessions.revoke_session(session, result.context.session.id, actor=admin)
    cb = _callback()
    await teacher_chats.open_thread(
        cb, teacher_chats.AuThreadCb(action="open", thread_id=thread.id), session, teacher_user
    )
    cb.message.edit_text.assert_not_awaited()
    assert "Maxfiy" not in _sent(cb)


async def test_bot_teacher_sees_only_own_threads_with_chat_right(session, make_user) -> None:
    teacher_user, _, account, course, thread = await _chat_setup(session, make_user)
    other_user = await make_user()
    other = await make_teacher(session, login="teacher2", user=other_user)
    await assign(session, other, course, can_chat=True)
    await _login(session, other_user, login="teacher2")
    with pytest.raises(NotFound):
        await teacher_chats.open_thread(
            _callback(), teacher_chats.AuThreadCb(action="open", thread_id=thread.id), session, other_user
        )

    await _login(session, teacher_user)
    assignment = await session.scalar(
        select(TeacherCourseAssignment).where(TeacherCourseAssignment.teacher_id == account.id)
    )
    assignment.can_chat = False
    await session.flush()
    with pytest.raises(AccessDenied):
        await teacher_chats.open_thread(
            _callback(), teacher_chats.AuThreadCb(action="open", thread_id=thread.id), session, teacher_user
        )


async def test_bot_teacher_reply_delivered_to_student(session, make_user, monkeypatch) -> None:
    teacher_user, student, _, _, thread = await _chat_setup(session, make_user)
    await _login(session, teacher_user)
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(chat, "deliver_to_student", delivered)
    state = _state(teacher_user)
    await state.update_data(au_thread=thread.id)
    await state.set_state(teacher_chats.AuTeacherReply.body)
    msg = _text_message("Javob")
    await teacher_chats.reply_send(msg, NullBot(), session, teacher_user, state)
    assert delivered.await_args.args[1].id == student.id
    assert "Yuborildi" in _sent(msg)


# --------------------------------------------------------------------------- bot handlers


def _message() -> AsyncMock:
    return AsyncMock()


def _callback() -> AsyncMock:
    cb = AsyncMock()
    cb.message = AsyncMock()
    cb.message.__class__ = Message  # passes the handlers' isinstance(..., Message) checks
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
        teacher_chats.chats_entry,
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


async def test_bot_logout_command(session, make_user, monkeypatch) -> None:
    user = await make_user()
    await make_teacher(session)
    result = await _login(session, user)
    purge = AsyncMock()
    monkeypatch.setattr(teacher_trail, "purge", purge)
    msg = _message()
    bot = NullBot()
    await cabinet.cmd_logout(msg, bot, session, user)
    assert await teacher_sessions.get_active_session(session, user.telegram_id) is None
    purge.assert_awaited_once_with(bot, result.context.session.id)  # chat trail wiped (TZ §7.4)


async def test_trail_sweep_purges_expired_session_only(session, make_user, clock, monkeypatch) -> None:
    """After 24h what the bot showed the teacher is deleted from Telegram (TZ §7.4–7.5)."""
    user = await make_user()
    other = await make_user()
    await make_teacher(session)
    await make_teacher(session, login="teacher2")
    old = (await _login(session, user)).context.session
    clock.travel(timedelta(hours=12))
    fresh = (await _login(session, other, login="teacher2")).context.session
    purged: list[int] = []

    async def fake_purge(bot, session_id):
        purged.append(session_id)
        return 1

    async def keys(match):
        for sid in (old.id, fresh.id):
            yield teacher_trail.key(sid)

    monkeypatch.setattr(teacher_trail, "purge", fake_purge)
    monkeypatch.setattr(teacher_trail, "_redis", lambda: type("R", (), {"scan_iter": staticmethod(keys)})())

    assert await teacher_trail.sweep(NullBot(), session, now=clock.now()) == 0
    clock.travel(timedelta(hours=12, seconds=1))  # `old` is now past its strict 24h
    assert await teacher_trail.sweep(NullBot(), session, now=clock.now()) == 1
    assert purged == [old.id]


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
