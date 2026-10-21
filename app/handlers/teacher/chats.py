"""💬 Teacher ↔ student chats inside the bot (TZ §7.5). OWNER: AUTH.

Every handler calls `require_teacher` first: after 24h, logout or an admin revoke nothing here
works until the teacher logs in again. Messages live in the bot's own DB (teacher_chat_*).
"""

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChatSenderRole, Course, User
from app.handlers.teacher.cabinet import _ctx
from app.keyboards import menus
from app.services import chat
from app.services.teacher_sessions import TeacherContext
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="teacher.chats")

TEXT_LIMIT = 3800  # Telegram allows 4096 characters per message


class AuThreadCb(CallbackData, prefix="au_th"):
    action: str  # list | open | reply
    thread_id: int = 0


class AuTeacherReply(StatesGroup):
    body = State()


def _button(text: str, data: AuThreadCb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


async def _render_list(session: AsyncSession, ctx: TeacherContext) -> tuple[str, InlineKeyboardMarkup | None]:
    threads = await chat.teacher_threads(session, ctx)
    if not threads:
        return "💬 Hozircha o‘quvchilardan yozishmalar yo‘q.", None
    rows = []
    for t in threads[:40]:
        unread = f" 🔵{t.unread}" if t.unread else ""
        course = f" · {t.course_title}" if t.course_title else ""
        rows.append(
            [
                _button(
                    truncate(f"{t.student_name}{course}{unread}", 60),
                    AuThreadCb(action="open", thread_id=t.id),
                )
            ]
        )
    return "💬 <b>Yozishmalar</b>\n\nSuhbatni tanlang:", InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_thread(
    session: AsyncSession, ctx: TeacherContext, thread_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    thread = await chat.teacher_thread(session, ctx, thread_id)
    student = await session.get(User, thread.student_user_id)
    course_title = await session.scalar(select(Course.title).where(Course.id == thread.course_id))
    messages = await chat.thread_messages(session, ctx, thread)
    name = student.full_name if student else "O‘quvchi"
    header = f"💬 <b>{esc(name)}</b>" + (f" · {esc(course_title)}" if course_title else "")
    parts: list[str] = []
    for m in messages:
        who = "👨‍🏫 Siz" if m.sender_role == ChatSenderRole.TEACHER else f"👨‍🎓 {esc(name)}"
        parts.append(f"<b>{who}</b> <i>{fmt_dt(m.created_at)}</i>\n{esc(m.body)}")
    body = "\n\n".join(parts) or "Xabarlar yo‘q."
    while len(body) > TEXT_LIMIT and "\n\n" in body:  # keep the newest messages
        body = body.split("\n\n", 1)[1]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("↩️ Javob yozish", AuThreadCb(action="reply", thread_id=thread.id))],
            [
                _button("🔄 Yangilash", AuThreadCb(action="open", thread_id=thread.id)),
                _button("⬅️ Ro‘yxat", AuThreadCb(action="list")),
            ],
        ]
    )
    return f"{header}\n\n{body}", keyboard


@router.message(F.text == menus.BTN_T_CHATS)
async def chats_entry(message: Message, session: AsyncSession, user: User) -> None:
    ctx = await _ctx(message, session, user)
    if ctx is None:
        return
    text, keyboard = await _render_list(session, ctx)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(AuThreadCb.filter(F.action == "list"))
async def chats_list(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    ctx = await _ctx(callback, session, user)
    if ctx is None:
        return
    text, keyboard = await _render_list(session, ctx)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AuThreadCb.filter(F.action == "open"))
async def open_thread(
    callback: CallbackQuery, callback_data: AuThreadCb, session: AsyncSession, user: User
) -> None:
    ctx = await _ctx(callback, session, user)
    if ctx is None:
        return
    text, keyboard = await _render_thread(session, ctx, callback_data.thread_id)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except Exception:  # noqa: BLE001 - "message is not modified" on refresh without news
            pass
    await callback.answer()


@router.callback_query(AuThreadCb.filter(F.action == "reply"))
async def reply_start(
    callback: CallbackQuery, callback_data: AuThreadCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    ctx = await _ctx(callback, session, user)
    if ctx is None:
        return
    await chat.teacher_thread(session, ctx, callback_data.thread_id)  # access check
    await state.set_state(AuTeacherReply.body)
    await state.update_data(au_thread=callback_data.thread_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Javobingizni yozing (bekor qilish: /menu).")


@router.message(AuTeacherReply.body, F.text)
async def reply_send(
    message: Message, bot: Bot, session: AsyncSession, user: User, state: FSMContext
) -> None:
    ctx = await _ctx(message, session, user)
    if ctx is None:
        await state.clear()
        return
    thread_id = int((await state.get_data()).get("au_thread") or 0)
    thread = await chat.teacher_thread(session, ctx, thread_id)
    msg, student = await chat.teacher_send(session, ctx, user, thread, message.text or "")
    course_title = await session.scalar(select(Course.title).where(Course.id == thread.course_id))
    teacher_name = ctx.teacher.full_name
    await state.clear()
    await session.commit()
    delivered = False
    if student is not None:
        delivered = await chat.deliver_to_student(
            bot,
            student,
            teacher_name=teacher_name,
            course_title=course_title,
            thread_id=thread.id,
            body=msg.body,
        )
    await message.answer("✅ Yuborildi." if delivered else "✅ Saqlandi (o‘quvchiga yetkazib bo‘lmadi).")
    text, keyboard = await _render_thread(session, ctx, thread.id)
    await message.answer(text, reply_markup=keyboard)
