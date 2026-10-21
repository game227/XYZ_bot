"""Teacher <-> student chat stored in the bot's own DB (TZ §7.5). OWNER: AUTH.

- Students write through the bot; teacher replies are delivered to the student by the bot.
- Teachers read and write in the bot (💬 Chatlar), and only with a valid session
  (`require_teacher` on every bot handler) and the `can_chat` right for the thread's course.
- New student messages trigger only a text-free "yangi xabar bor" notice; the conversation
  itself is shown inside 💬 Chatlar, which requires a valid session.
- Session end never deletes chat history.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ChatSenderRole,
    Course,
    CourseEnrollment,
    TeacherAccount,
    TeacherChatMessage,
    TeacherChatThread,
    TeacherCourseAssignment,
    User,
    UserStatus,
)
from app.keyboards.auth import AuChatCb
from app.services import teacher_sessions
from app.services.errors import AccessDenied, InvalidInput, NotFound
from app.services.subscriptions import has_active_subscription
from app.utils.text import esc
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 2000
PAGE_SIZE = 50
TEACHER_NOTICE_COOLDOWN_SECONDS = 60
NEW_MESSAGE_NOTICE = (
    "💬 Sizga o‘quvchidan yangi xabar bor. Ko‘rish uchun o‘qituvchi kabinetida «💬 Chatlar» bo‘limini oching."
)


def write_to_teacher_button(course_id: int, text: str = "✉️ O‘qituvchiga yozish") -> InlineKeyboardButton:
    """Student entry point to the chat (CONTENT may place it on course screens)."""
    return InlineKeyboardButton(text=text, callback_data=AuChatCb(action="start", course_id=course_id).pack())


def clean_body(body: str | None) -> str:
    body = (body or "").strip()
    if not body:
        raise InvalidInput("Xabar bo‘sh bo‘lmasligi kerak.")
    if len(body) > MAX_MESSAGE_LENGTH:
        raise InvalidInput(f"Xabar {MAX_MESSAGE_LENGTH} belgidan oshmasligi kerak.")
    return body


# --------------------------------------------------------------------------- student side


async def chat_teachers_for_course(session: AsyncSession, course_id: int) -> list[TeacherAccount]:
    """Teachers a student may write to about this course: assigned with `can_chat`, bound, not blocked."""
    stmt = (
        select(TeacherAccount)
        .join(TeacherCourseAssignment, TeacherCourseAssignment.teacher_id == TeacherAccount.id)
        .where(
            TeacherCourseAssignment.course_id == course_id,
            TeacherCourseAssignment.can_chat.is_(True),
            TeacherAccount.is_blocked.is_(False),
            TeacherAccount.user_id.is_not(None),
        )
        .order_by(TeacherAccount.full_name)
    )
    return list((await session.scalars(stmt)).all())


async def require_student_can_write(session: AsyncSession, student: User, course_id: int) -> Course:
    """Only students of the course (enrolled, or active subscribers) may start a chat."""
    course = await session.get(Course, course_id)
    if course is None or not course.is_active:
        raise NotFound("Kurs topilmadi.")
    enrolled = await session.scalar(
        select(CourseEnrollment.id).where(
            CourseEnrollment.user_id == student.id, CourseEnrollment.course_id == course_id
        )
    )
    if enrolled is None and not await has_active_subscription(session, student.id):
        raise AccessDenied("O‘qituvchiga yozish uchun avval shu kursdagi darslardan birini oching.")
    return course


async def get_or_create_thread(
    session: AsyncSession, *, teacher_id: int, student_user_id: int, course_id: int
) -> TeacherChatThread:
    await session.execute(
        insert(TeacherChatThread)
        .values(teacher_id=teacher_id, student_user_id=student_user_id, course_id=course_id)
        .on_conflict_do_nothing()
    )
    thread = await session.scalar(
        select(TeacherChatThread).where(
            TeacherChatThread.teacher_id == teacher_id,
            TeacherChatThread.student_user_id == student_user_id,
            TeacherChatThread.course_id == course_id,
        )
    )
    assert thread is not None
    return thread


async def _thread_teacher_can_chat(session: AsyncSession, thread: TeacherChatThread) -> TeacherAccount | None:
    """The thread's teacher if the thread is still usable (course assigned with can_chat, not blocked)."""
    if thread.course_id is None or thread.is_closed:
        return None
    teachers = await chat_teachers_for_course(session, thread.course_id)
    return next((t for t in teachers if t.id == thread.teacher_id), None)


async def student_thread(session: AsyncSession, student: User, thread_id: int) -> TeacherChatThread:
    thread = await session.get(TeacherChatThread, thread_id)
    if thread is None or thread.student_user_id != student.id:
        raise NotFound("Suhbat topilmadi.")
    return thread


async def student_send(
    session: AsyncSession, student: User, thread: TeacherChatThread, body: str
) -> tuple[TeacherChatMessage, int | None]:
    """Store a student message. Returns (message, teacher_telegram_id_to_notify)."""
    body = clean_body(body)
    if thread.student_user_id != student.id:
        raise NotFound("Suhbat topilmadi.")
    teacher = await _thread_teacher_can_chat(session, thread)
    if teacher is None:
        raise AccessDenied("Bu o‘qituvchiga hozircha yozib bo‘lmaydi.")
    msg = await _add_message(session, thread, student, ChatSenderRole.STUDENT, body)
    teacher_tg = await session.scalar(select(User.telegram_id).where(User.id == teacher.user_id))
    return msg, teacher_tg


async def _add_message(
    session: AsyncSession, thread: TeacherChatThread, sender: User, role: ChatSenderRole, body: str
) -> TeacherChatMessage:
    now = utcnow()
    msg = TeacherChatMessage(
        thread_id=thread.id, sender_user_id=sender.id, sender_role=role, body=body, created_at=now
    )
    session.add(msg)
    thread.last_message_at = now
    await session.flush()
    return msg


# --------------------------------------------------------------------------- teacher side


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    id: int
    student_name: str
    student_username: str | None
    course_id: int | None
    course_title: str | None
    last_message_at: datetime | None
    unread: int


async def teacher_threads(session: AsyncSession, ctx: teacher_sessions.TeacherContext) -> list[ThreadSummary]:
    course_ids = await teacher_sessions.teacher_course_ids(session, ctx.teacher.id, "can_chat")
    if not course_ids:
        return []
    unread = (
        select(func.count(TeacherChatMessage.id))
        .where(
            TeacherChatMessage.thread_id == TeacherChatThread.id,
            TeacherChatMessage.sender_role == ChatSenderRole.STUDENT,
            TeacherChatMessage.read_at.is_(None),
        )
        .correlate(TeacherChatThread)
        .scalar_subquery()
    )
    stmt = (
        select(TeacherChatThread, User, Course.title, unread)
        .join(User, User.id == TeacherChatThread.student_user_id)
        .join(Course, Course.id == TeacherChatThread.course_id)
        .where(
            TeacherChatThread.teacher_id == ctx.teacher.id,
            TeacherChatThread.course_id.in_(course_ids),
            TeacherChatThread.last_message_at.is_not(None),
        )
        .order_by(TeacherChatThread.last_message_at.desc())
        .limit(200)
    )
    return [
        ThreadSummary(
            id=thread.id,
            student_name=student.full_name,
            student_username=student.username,
            course_id=thread.course_id,
            course_title=course_title,
            last_message_at=thread.last_message_at,
            unread=int(unread_count or 0),
        )
        for thread, student, course_title, unread_count in (await session.execute(stmt)).all()
    ]


async def teacher_thread(
    session: AsyncSession, ctx: teacher_sessions.TeacherContext, thread_id: int
) -> TeacherChatThread:
    """The thread if it belongs to this teacher and its course still grants `can_chat`."""
    thread = await session.get(TeacherChatThread, thread_id)
    if thread is None or thread.teacher_id != ctx.teacher.id or thread.course_id is None:
        raise NotFound("Suhbat topilmadi.")
    await teacher_sessions.require_course_right(session, ctx, thread.course_id, "can_chat")
    return thread


async def thread_messages(
    session: AsyncSession,
    ctx: teacher_sessions.TeacherContext,
    thread: TeacherChatThread,
    *,
    after_id: int = 0,
    before_id: int | None = None,
    limit: int = PAGE_SIZE,
) -> list[TeacherChatMessage]:
    """Messages in ascending id order; marks the student's messages as read by the teacher."""
    assert thread.teacher_id == ctx.teacher.id
    limit = max(1, min(limit, PAGE_SIZE))
    stmt = select(TeacherChatMessage).where(TeacherChatMessage.thread_id == thread.id)
    if before_id is not None:
        stmt = stmt.where(TeacherChatMessage.id < before_id).order_by(TeacherChatMessage.id.desc())
    elif after_id:
        stmt = stmt.where(TeacherChatMessage.id > after_id).order_by(TeacherChatMessage.id.asc())
    else:
        stmt = stmt.order_by(TeacherChatMessage.id.desc())
    messages = list((await session.scalars(stmt.limit(limit))).all())
    messages.sort(key=lambda m: m.id)
    if messages:
        await session.execute(
            update(TeacherChatMessage)
            .where(
                and_(
                    TeacherChatMessage.thread_id == thread.id,
                    TeacherChatMessage.sender_role == ChatSenderRole.STUDENT,
                    TeacherChatMessage.read_at.is_(None),
                    TeacherChatMessage.id <= messages[-1].id,
                )
            )
            .values(read_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    return messages


async def teacher_send(
    session: AsyncSession,
    ctx: teacher_sessions.TeacherContext,
    teacher_user: User,
    thread: TeacherChatThread,
    body: str,
) -> tuple[TeacherChatMessage, User | None]:
    """Store a teacher reply. Returns (message, student_to_deliver_to or None if they can't receive)."""
    body = clean_body(body)
    assert thread.teacher_id == ctx.teacher.id
    if thread.is_closed:
        raise AccessDenied("Suhbat yopilgan.")
    msg = await _add_message(session, thread, teacher_user, ChatSenderRole.TEACHER, body)
    student = await session.get(User, thread.student_user_id)
    if student is None or student.status != UserStatus.ACTIVE:
        return msg, None
    return msg, student


async def unread_threads_count(session: AsyncSession, ctx: teacher_sessions.TeacherContext) -> int:
    course_ids = await teacher_sessions.teacher_course_ids(session, ctx.teacher.id, "can_chat")
    if not course_ids:
        return 0
    stmt = (
        select(func.count(func.distinct(TeacherChatMessage.thread_id)))
        .join(TeacherChatThread, TeacherChatThread.id == TeacherChatMessage.thread_id)
        .where(
            TeacherChatThread.teacher_id == ctx.teacher.id,
            TeacherChatThread.course_id.in_(course_ids),
            TeacherChatMessage.sender_role == ChatSenderRole.STUDENT,
            TeacherChatMessage.read_at.is_(None),
        )
    )
    return int(await session.scalar(stmt) or 0)


# --------------------------------------------------------------------------- delivery (call after commit)


async def notify_teacher(bot: Bot, teacher_telegram_id: int, *, teacher_id: int) -> None:
    """Text-free notice to the teacher (no message content), at most once per cooldown."""
    try:
        from app.services.auth import get_redis

        first = await get_redis().set(
            f"au:chat:notice:{teacher_id}", 1, ex=TEACHER_NOTICE_COOLDOWN_SECONDS, nx=True
        )
        if not first:
            return
    except Exception:  # noqa: BLE001 - debounce is best effort
        logger.warning("chat notice debounce unavailable", exc_info=True)
    try:
        await bot.send_message(teacher_telegram_id, NEW_MESSAGE_NOTICE)
    except Exception:  # noqa: BLE001 - delivery problems must not fail the student's send
        logger.warning("teacher chat notice failed for teacher %s", teacher_id, exc_info=True)


async def deliver_to_student(
    bot: Bot, student: User, *, teacher_name: str, course_title: str | None, thread_id: int, body: str
) -> bool:
    course = f" · {esc(course_title)}" if course_title else ""
    text = f"💬 <b>O‘qituvchi {esc(teacher_name)}</b>{course}:\n\n{esc(body)}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Javob yozish", callback_data=AuChatCb(action="reply", thread_id=thread_id).pack()
                )
            ]
        ]
    )
    try:
        await bot.send_message(student.telegram_id, text, reply_markup=markup)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("chat delivery to student %s failed", student.id, exc_info=True)
        return False
