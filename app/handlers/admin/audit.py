"""Admin: audit log viewer (TZ §16)."""

import json

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminAuditLog, AdminPermission, User
from app.keyboards import menus
from app.services.access import AdminFilter, require_admin
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="admin.audit")

PAGE_SIZE = 10


class CoAuditCb(CallbackData, prefix="co_aud"):
    page: int


def _format_entry(entry: AdminAuditLog) -> str:
    actor = f"{entry.actor_telegram_id}" if entry.actor_telegram_id else "tizim"
    target = f" · {entry.entity_type}#{entry.entity_id}" if entry.entity_type else ""
    details = ""
    if entry.details:
        details = "\n   " + esc(truncate(json.dumps(entry.details, ensure_ascii=False), 200))
    header = f"<code>{fmt_dt(entry.created_at)}</code> <b>{esc(entry.action)}</b>{esc(target)}"
    return f"{header}\n   👤 {actor}{details}"


def _pager(page: int, has_next: bool) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="⬅️ Yangiroq", callback_data=CoAuditCb(page=page - 1))
    if has_next:
        kb.button(text="Eskiroq ➡️", callback_data=CoAuditCb(page=page + 1))
    return kb.as_markup() if page > 0 or has_next else None


async def _render(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    entries = list(
        (
            await session.scalars(
                select(AdminAuditLog)
                .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE + 1)
            )
        ).all()
    )
    has_next = len(entries) > PAGE_SIZE
    entries = entries[:PAGE_SIZE]
    total = await session.scalar(select(func.count()).select_from(AdminAuditLog))
    if not entries:
        return "<b>🧾 Audit jurnali</b>\n\nYozuvlar yo‘q.", None
    body = "\n\n".join(_format_entry(e) for e in entries)
    return f"<b>🧾 Audit jurnali</b> (jami {total}, sahifa {page + 1})\n\n{body}", _pager(page, has_next)


@router.message(F.text == menus.BTN_A_AUDIT, AdminFilter(AdminPermission.AUDIT))
async def audit_menu(message: Message, user: User, session: AsyncSession) -> None:
    require_admin(user, AdminPermission.AUDIT)
    text, markup = await _render(session, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(CoAuditCb.filter())
async def audit_page(
    callback: CallbackQuery, callback_data: CoAuditCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.AUDIT)
    text, markup = await _render(session, max(callback_data.page, 0))
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()
