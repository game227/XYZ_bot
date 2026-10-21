"""Student: 🔔 Bildirishnomalar — inbox of received notifications + on/off switch (TZ §10)."""

import html
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards import menus
from app.keyboards.commerce import CmInboxCb, btn, pager
from app.services import notifications
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="student.notifications")

PER_PAGE = 5
_TAG = re.compile(r"<[^>]+>")


def _plain(body: str, limit: int) -> str:
    """Bodies are stored as HTML; cutting them could break a tag or entity, so shorten the plain text."""
    return esc(truncate(html.unescape(_TAG.sub("", body)), limit))


async def _inbox_screen(session: AsyncSession, user: User, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items, total = await notifications.inbox(session, user.id, limit=PER_PAGE, offset=page * PER_PAGE)
    enabled = await notifications.notifications_enabled(session, user.id)
    lines = ["🔔 <b>Bildirishnomalar</b>"]
    if not enabled:
        lines.append("<i>E’lonlar o‘chirilgan. Obuna va to‘lov xabarlari baribir keladi.</i>")
    if not items:
        lines += ["", "Hozircha bildirishnomalar yo‘q."]
    for item in items:
        label = notifications.KIND_LABELS.get(item.kind, "🔔")
        lines += ["", f"<b>{label}</b> · {fmt_dt(item.created_at)}", _plain(item.body, 400)]
    nav = pager(CmInboxCb, page, total, PER_PAGE, action="page")
    toggle = btn(
        "🔕 E’lonlarni o‘chirish" if enabled else "🔔 E’lonlarni yoqish",
        CmInboxCb(action="toggle", page=page),
    )
    keyboard = [nav] if nav else []
    keyboard.append([toggle])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(F.text == menus.BTN_NOTIFICATIONS)
async def inbox_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _inbox_screen(session, user, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmInboxCb.filter(F.action == "page"))
async def inbox_page(
    callback: CallbackQuery, callback_data: CmInboxCb, session: AsyncSession, user: User
) -> None:
    text, kb = await _inbox_screen(session, user, max(callback_data.page, 0))
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(CmInboxCb.filter(F.action == "toggle"))
async def inbox_toggle(
    callback: CallbackQuery, callback_data: CmInboxCb, session: AsyncSession, user: User
) -> None:
    enabled = await notifications.notifications_enabled(session, user.id)
    await notifications.set_notifications_enabled(session, user.id, not enabled)
    await callback.answer("E’lonlar yoqildi" if not enabled else "E’lonlar o‘chirildi")
    text, kb = await _inbox_screen(session, user, max(callback_data.page, 0))
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)
