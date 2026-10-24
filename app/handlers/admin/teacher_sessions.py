"""Admin: active teacher sessions and revocation (TZ §7.3, §13.3, §22.9). OWNER: AUTH.

Revocation is a DB update; every teacher handler / endpoint re-reads the session, so the
teacher's very next request is rejected.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, TeacherAccount, TeacherSession, User
from app.keyboards import menus
from app.keyboards.auth import AuSessionCb, pager_row
from app.services import teacher_sessions, teacher_trail
from app.services.access import AdminFilter, require_admin
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt, utcnow

logger = logging.getLogger(__name__)

router = Router(name="admin.teacher_sessions")

PERM = AdminPermission.TEACHER_SESSIONS
PAGE = 10


async def _list(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup]:
    now = utcnow()
    total = await session.scalar(
        select(func.count(TeacherSession.id)).where(
            TeacherSession.revoked_at.is_(None), TeacherSession.expires_at > now
        )
    )
    items = await teacher_sessions.list_active_sessions(session, now=now, limit=PAGE + 1, offset=page * PAGE)
    has_next = len(items) > PAGE
    buttons = [
        [
            InlineKeyboardButton(
                text=truncate(f"🟢 {teacher.full_name} · {fmt_dt(ts.expires_at)} gacha", 60),
                callback_data=AuSessionCb(action="view", session_id=ts.id, page=page).pack(),
            )
        ]
        for ts, teacher in items[:PAGE]
    ]
    nav = pager_row(page, has_next, lambda p: AuSessionCb(action="list", page=p).pack())
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Yangilash", callback_data=AuSessionCb(action="list", page=page).pack()
            )
        ]
    )
    text = f"<b>🔐 Faol o‘qituvchi sessiyalari</b>: {total or 0}"
    if not items:
        text += "\n\nHozir faol sessiya yo‘q."
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_TEACHER_SESSIONS)
async def sessions_menu(message: Message, session: AsyncSession, user: User) -> None:
    require_admin(user, PERM)
    text, markup = await _list(session, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(AuSessionCb.filter(F.action == "list"))
async def sessions_list(
    callback: CallbackQuery, callback_data: AuSessionCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, markup = await _list(session, max(callback_data.page, 0))
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(AuSessionCb.filter(F.action == "view"))
async def session_view(
    callback: CallbackQuery, callback_data: AuSessionCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    row = (
        await session.execute(
            select(TeacherSession, TeacherAccount)
            .join(TeacherAccount, TeacherAccount.id == TeacherSession.teacher_id)
            .where(TeacherSession.id == callback_data.session_id)
        )
    ).first()
    if row is None:
        await callback.answer("Sessiya topilmadi.", show_alert=True)
        return
    ts, teacher = row
    now = utcnow()
    active = ts.revoked_at is None and ts.expires_at > now
    if ts.revoked_at is not None:
        state = f"yakunlangan ({fmt_dt(ts.revoked_at)}, {ts.end_reason or '—'})"
    elif not active:
        state = "muddati tugagan"
    else:
        left = ts.expires_at - now
        state = f"faol, {left.seconds // 3600 + left.days * 24} soat {left.seconds % 3600 // 60} daqiqa qoldi"
    text = (
        f"<b>🔐 Sessiya #{ts.id}</b>\n"
        f"O‘qituvchi: {esc(teacher.full_name)} (<code>{esc(teacher.login)}</code>)\n"
        f"Telegram ID: <code>{ts.telegram_id}</code>\n"
        f"Boshlangan: {fmt_dt(ts.created_at)}\n"
        f"Tugaydi: {fmt_dt(ts.expires_at)}\n"
        f"Holat: {state}"
    )
    buttons = []
    if active:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⛔️ Sessiyani bekor qilish",
                    callback_data=AuSessionCb(
                        action="revoke", session_id=ts.id, page=callback_data.page
                    ).pack(),
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Ro‘yxat", callback_data=AuSessionCb(action="list", page=callback_data.page).pack()
            )
        ]
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(AuSessionCb.filter(F.action == "revoke"))
async def session_revoke(
    callback: CallbackQuery, callback_data: AuSessionCb, session: AsyncSession, user: User, bot: Bot
) -> None:
    require_admin(user, PERM)
    if not callback_data.confirm:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Ha, bekor qilish",
                        callback_data=AuSessionCb(
                            action="revoke",
                            session_id=callback_data.session_id,
                            page=callback_data.page,
                            confirm=1,
                        ).pack(),
                    ),
                    InlineKeyboardButton(
                        text="❌ Yo‘q",
                        callback_data=AuSessionCb(
                            action="view", session_id=callback_data.session_id, page=callback_data.page
                        ).pack(),
                    ),
                ]
            ]
        )
        await callback.message.edit_text(
            "Sessiyani bekor qilasizmi? O‘qituvchi darhol chiqariladi.", reply_markup=markup
        )
        await callback.answer()
        return

    ts = await teacher_sessions.revoke_session(session, callback_data.session_id, actor=user)
    await session.commit()
    if ts is None:
        await callback.answer("Sessiya allaqachon yakunlangan.", show_alert=True)
    else:
        await callback.answer("Sessiya bekor qilindi.")
        await teacher_trail.purge(bot, ts.id)
        try:
            await bot.send_message(
                ts.telegram_id,
                "🔒 O‘qituvchi sessiyangiz administrator tomonidan yakunlandi. Qayta kirishingiz mumkin.",
                reply_markup=menus.teacher_menu(logged_in=False),
            )
        except Exception:  # noqa: BLE001 - notification is best effort
            logger.info("revoke notice failed", exc_info=True)
    text, markup = await _list(session, max(callback_data.page, 0))
    await callback.message.edit_text(text, reply_markup=markup)
