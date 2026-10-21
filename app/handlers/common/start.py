from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TeacherAccount, User, UserRole
from app.keyboards import menus
from app.services import bot_settings
from app.services.access import is_admin, is_superadmin
from app.services.subscriptions import get_active_subscription
from app.utils.text import esc
from app.utils.time import fmt_dt

router = Router(name="common.start")


async def role_labels(session: AsyncSession, user: User) -> list[str]:
    """One account can be a student, a teacher (bound teacher account) and an admin at once."""
    labels = ["O‘quvchi"]
    if await session.scalar(select(TeacherAccount.id).where(TeacherAccount.user_id == user.id)):
        labels.append("O‘qituvchi")
    if is_admin(user):
        labels.append("Superadmin" if is_superadmin(user) else "Administrator")
    return labels


def main_menu_for(user: User) -> ReplyKeyboardMarkup:
    """Everyone, admins included, uses the bot as a regular user. Admins get an extra
    «🛠 Admin» button that opens the admin panel; teachers get a switch to their menu."""
    return menus.student_menu(
        show_admin_switch=is_admin(user), show_teacher_switch=user.role == UserRole.TEACHER
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"Assalomu alaykum, <b>{esc(user.first_name or user.full_name)}</b>!\n\n"
        "XYZ ta’lim botiga xush kelibsiz. Quyidagi menyudan bo‘limni tanlang.",
        reply_markup=main_menu_for(user),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Asosiy menyu:", reply_markup=main_menu_for(user))


@router.message(F.text == menus.BTN_STUDENT_MENU)
async def to_student_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Asosiy menyu:", reply_markup=main_menu_for(user))


@router.message(F.text == menus.BTN_ADMIN_MENU)
async def to_admin_menu(message: Message, user: User, state: FSMContext) -> None:
    if not is_admin(user):
        return
    await state.clear()
    await message.answer("Admin panel:", reply_markup=menus.admin_menu())


@router.message(Command("help"))
@router.message(F.text == menus.BTN_HELP)
async def cmd_help(message: Message, session: AsyncSession) -> None:
    text = esc(await bot_settings.get_setting(session, bot_settings.HELP_TEXT))
    contact = await bot_settings.get_setting(session, bot_settings.SUPPORT_CONTACT)
    if contact:
        text += f"\n\nAloqa: {esc(contact)}"
    await message.answer(text)


@router.message(Command("profile"))
@router.message(F.text == menus.BTN_PROFILE)
async def cmd_profile(message: Message, user: User, session: AsyncSession) -> None:
    sub = await get_active_subscription(session, user.id)
    sub_line = f"faol, {fmt_dt(sub.ends_at)} gacha" if sub else "faol emas"
    username = f"@{esc(user.username)}" if user.username else "—"
    await message.answer(
        "<b>👤 Profil</b>\n\n"
        f"Ism: {esc(user.full_name)}\n"
        f"Username: {username}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Rollar: {', '.join(await role_labels(session, user))}\n"
        f"Obuna: {sub_line}\n"
        f"Ro‘yxatdan o‘tgan: {fmt_dt(user.created_at)}"
    )
