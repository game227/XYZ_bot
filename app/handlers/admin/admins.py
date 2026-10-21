"""Admin: 👑 Adminlar — add an admin by Telegram user ID, remove admins (TZ §3.1)."""

import logging

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import AdminPermission, User
from app.keyboards import menus
from app.services import admins
from app.services.access import AdminFilter, require_admin
from app.utils.text import esc, truncate

logger = logging.getLogger(__name__)

router = Router(name="admin.admins")


class CoAdminAdd(StatesGroup):
    telegram_id = State()


class CoAdminCb(CallbackData, prefix="co_adm"):
    action: str  # list | add | ask_remove | remove
    user_id: int = 0


def _name(user: User) -> str:
    username = f" @{user.username}" if user.username else ""
    shown = user.full_name if (user.first_name or user.username) else "hali /start bosmagan"
    return f"{shown}{username} · {user.telegram_id}"


async def _render(session: AsyncSession, actor: User) -> tuple[str, InlineKeyboardMarkup]:
    superadmins = get_settings().superadmin_ids
    others = [u for u in await admins.list_admins(session) if u.telegram_id not in superadmins]
    lines = ["<b>👑 Adminlar</b>", ""]
    lines += [f"⭐ Superadmin: <code>{tid}</code>" for tid in superadmins]
    kb = InlineKeyboardBuilder()
    if others:
        lines.append("")
        for n, u in enumerate(others, start=1):
            lines.append(f"{n}. {esc(_name(u))}")
            if u.id != actor.id:
                kb.button(
                    text=f"❌ {truncate(_name(u), 50)}",
                    callback_data=CoAdminCb(action="ask_remove", user_id=u.id),
                )
    else:
        lines.append("\nBoshqa adminlar yo‘q.")
    kb.button(text="➕ Admin qo‘shish", callback_data=CoAdminCb(action="add"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@router.message(F.text == menus.BTN_A_ADMINS, AdminFilter(AdminPermission.ADMINS))
async def admins_menu(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    require_admin(user, AdminPermission.ADMINS)
    await state.clear()
    text, kb = await _render(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CoAdminCb.filter(F.action == "list"))
async def admins_list(callback: CallbackQuery, user: User, session: AsyncSession) -> None:
    require_admin(user, AdminPermission.ADMINS)
    text, kb = await _render(session, user)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(CoAdminCb.filter(F.action == "add"))
async def add_start(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, AdminPermission.ADMINS)
    await state.set_state(CoAdminAdd.telegram_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Yangi adminning Telegram user ID raqamini yuboring (masalan 123456789).\n"
            "U o‘z ID sini @userinfobot orqali bilishi mumkin. Bekor qilish: /menu"
        )


@router.message(CoAdminAdd.telegram_id, F.text)
async def add_finish(
    message: Message, bot: Bot, user: User, session: AsyncSession, state: FSMContext
) -> None:
    require_admin(user, AdminPermission.ADMINS)
    telegram_id = admins.parse_telegram_id(message.text)
    new_admin, created = await admins.add_admin(session, user, telegram_id)
    await state.clear()
    await session.commit()
    notified = True
    try:
        await bot.send_message(
            telegram_id,
            "👑 Sizga admin huquqi berildi. Admin panelga «🛠 Admin» tugmasi orqali o‘tasiz.",
            reply_markup=menus.student_menu(show_admin_switch=True),
        )
    except Exception:  # noqa: BLE001 - e.g. the user hasn't started the bot yet
        notified = False
        logger.info("new admin %s could not be notified", telegram_id)
    note = (
        "U botga xabar oldi."
        if notified
        else "U hali botni ishga tushirmagan: /start bosgach «🛠 Admin» tugmasini ko‘radi."
    )
    await message.answer(f"✅ <code>{telegram_id}</code> admin qilindi. {note}")
    text, kb = await _render(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CoAdminCb.filter(F.action == "ask_remove"))
async def remove_ask(
    callback: CallbackQuery, callback_data: CoAdminCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.ADMINS)
    target = await session.get(User, callback_data.user_id)
    if target is None:
        await callback.answer("Topilmadi", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, olib tashlash", callback_data=CoAdminCb(action="remove", user_id=target.id))
    kb.button(text="⬅️ Bekor qilish", callback_data=CoAdminCb(action="list"))
    kb.adjust(1)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"{esc(_name(target))} adminlikdan olib tashlansinmi?", reply_markup=kb.as_markup()
        )
    await callback.answer()


@router.callback_query(CoAdminCb.filter(F.action == "remove"))
async def remove_confirm(
    callback: CallbackQuery, callback_data: CoAdminCb, bot: Bot, user: User, session: AsyncSession
) -> None:
    removed = await admins.remove_admin(session, user, callback_data.user_id)
    await session.commit()
    try:
        await bot.send_message(
            removed.telegram_id,
            "Sizning admin huquqingiz olib tashlandi.",
            reply_markup=menus.student_menu(),
        )
    except Exception:  # noqa: BLE001 - best effort
        pass
    text, kb = await _render(session, user)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Olib tashlandi")
