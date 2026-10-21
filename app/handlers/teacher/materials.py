"""🎬 Dars materiallari — teacher entry into the shared content editor (admin/content.py).

Every editor callback re-checks the teacher's active 24h session and course rights
(`lessons.ensure_can_manage_course` → AUTH's `require_teacher` + `can_manage_content`).
"""

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.admin.content import render_courses
from app.keyboards import menus
from app.services import teacher_sessions
from app.services.errors import TeacherSessionRequired

router = Router(name="teacher.materials")


async def require_teacher_or_relogin(message: Message, session: AsyncSession, user: User) -> bool:
    try:
        await teacher_sessions.require_teacher(session, user)
    except TeacherSessionRequired as exc:
        await message.answer(
            f"🔐 {exc.user_message}\n\nKirish uchun «{menus.BTN_T_LOGIN}» tugmasini bosing.",
            reply_markup=menus.teacher_menu(logged_in=False),
        )
        return False
    return True


@router.message(F.text == menus.BTN_T_MATERIALS)
async def materials_entry(message: Message, session: AsyncSession, user: User) -> None:
    if not await require_teacher_or_relogin(message, session, user):
        return
    text, kb = await render_courses(session, user)
    await message.answer(text, reply_markup=kb)
