"""Student side of the teacher chat (TZ §7.5). OWNER: AUTH.

Entry: `chat.write_to_teacher_button(course_id)` (course screens) and the "↩️ Javob yozish"
button under every teacher reply. Messages are stored in the bot DB; the teacher gets only a
text-free notice and reads them in the Mini App.
"""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TeacherAccount, User
from app.keyboards.auth import AuChatCb
from app.services import chat
from app.utils.text import esc, truncate

router = Router(name="student.teacher_chat")


class AuStudentChat(StatesGroup):
    writing = State()


CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=AuChatCb(action="cancel").pack())]
    ]
)


async def _prompt(callback: CallbackQuery, state: FSMContext, *, thread_id: int, teacher_name: str) -> None:
    await state.set_state(AuStudentChat.writing)
    await state.update_data(au_thread_id=thread_id)
    await callback.message.answer(
        f"✉️ O‘qituvchi <b>{esc(teacher_name)}</b>ga xabaringizni yozing (faqat matn, "
        f"{chat.MAX_MESSAGE_LENGTH} belgigacha):",
        reply_markup=CANCEL_KB,
    )
    await callback.answer()


@router.callback_query(AuChatCb.filter(F.action == "start"))
async def chat_start(
    callback: CallbackQuery, callback_data: AuChatCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await chat.require_student_can_write(session, user, callback_data.course_id)
    teachers = await chat.chat_teachers_for_course(session, callback_data.course_id)
    if not teachers:
        await callback.answer(
            "Bu kurs bo‘yicha hozircha yozish mumkin bo‘lgan o‘qituvchi yo‘q.", show_alert=True
        )
        return
    if len(teachers) > 1:
        buttons = [
            [
                InlineKeyboardButton(
                    text=truncate(t.full_name, 60),
                    callback_data=AuChatCb(
                        action="to", course_id=callback_data.course_id, teacher_id=t.id
                    ).pack(),
                )
            ]
            for t in teachers
        ]
        await callback.message.answer(
            "Qaysi o‘qituvchiga yozasiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()
        return
    teacher = teachers[0]
    thread = await chat.get_or_create_thread(
        session, teacher_id=teacher.id, student_user_id=user.id, course_id=callback_data.course_id
    )
    await _prompt(callback, state, thread_id=thread.id, teacher_name=teacher.full_name)


@router.callback_query(AuChatCb.filter(F.action == "to"))
async def chat_to_teacher(
    callback: CallbackQuery, callback_data: AuChatCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await chat.require_student_can_write(session, user, callback_data.course_id)
    teachers = await chat.chat_teachers_for_course(session, callback_data.course_id)
    teacher = next((t for t in teachers if t.id == callback_data.teacher_id), None)
    if teacher is None:
        await callback.answer("Bu o‘qituvchiga hozircha yozib bo‘lmaydi.", show_alert=True)
        return
    thread = await chat.get_or_create_thread(
        session, teacher_id=teacher.id, student_user_id=user.id, course_id=callback_data.course_id
    )
    await _prompt(callback, state, thread_id=thread.id, teacher_name=teacher.full_name)


@router.callback_query(AuChatCb.filter(F.action == "reply"))
async def chat_reply(
    callback: CallbackQuery, callback_data: AuChatCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    thread = await chat.student_thread(session, user, callback_data.thread_id)
    teacher = await session.get(TeacherAccount, thread.teacher_id)
    await _prompt(callback, state, thread_id=thread.id, teacher_name=teacher.full_name if teacher else "")


@router.callback_query(AuChatCb.filter(F.action == "cancel"))
async def chat_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Bekor qilindi.")
    await callback.answer()


@router.message(AuStudentChat.writing, F.text)
async def chat_message(
    message: Message, session: AsyncSession, user: User, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    thread = await chat.student_thread(session, user, int(data.get("au_thread_id") or 0))
    _, teacher_tg = await chat.student_send(session, user, thread, message.text or "")
    teacher_id = thread.teacher_id
    await state.clear()
    await session.commit()  # persist before any Telegram call
    await message.answer("✅ Xabaringiz o‘qituvchiga yuborildi. Javob shu yerga keladi.")
    if teacher_tg is not None:
        await chat.notify_teacher(bot, teacher_tg, teacher_id=teacher_id)


@router.message(AuStudentChat.writing)
async def chat_non_text(message: Message) -> None:
    await message.answer(
        "Faqat matnli xabar yuborish mumkin. Bekor qilish uchun /menu.", reply_markup=CANCEL_KB
    )
