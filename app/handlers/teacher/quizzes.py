"""🧪 Testlarni boshqarish — teacher entry into the shared quiz editor (admin/quizzes.py).

Rights are re-checked on every callback (`can_manage_quizzes` + active 24h session).
"""

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.admin.quizzes import render_courses
from app.handlers.teacher.materials import require_teacher_or_relogin
from app.keyboards import menus

router = Router(name="teacher.quizzes")


@router.message(F.text == menus.BTN_T_QUIZZES)
async def quizzes_entry(message: Message, session: AsyncSession, user: User) -> None:
    if not await require_teacher_or_relogin(message, session, user):
        return
    text, kb = await render_courses(session, user)
    await message.answer(text, reply_markup=kb)
