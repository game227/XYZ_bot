"""Admin: 📢 Bildirishnomalar — manual broadcasts to all / a role / a course / chosen users (TZ §10.2).

Sending is queued (arq worker): rate-limited, RetryAfter honoured, blocked users recorded.
"""

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AdminPermission,
    Course,
    Notification,
    NotificationAudience,
    NotificationStatus,
    User,
    UserRole,
)
from app.keyboards import menus
from app.keyboards.commerce import CmBcCb, btn, rows
from app.services import notifications
from app.services.access import AdminFilter, require_admin
from app.services.errors import InvalidInput, NotFound
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="admin.notifications")

PERM = AdminPermission.NOTIFICATIONS
STATUS_LABELS = {
    NotificationStatus.DRAFT: "📝 qoralama",
    NotificationStatus.QUEUED: "⏳ navbatda",
    NotificationStatus.SENDING: "📤 yuborilmoqda",
    NotificationStatus.DONE: "✅ yuborildi",
    NotificationStatus.FAILED: "⚠️ xato",
    NotificationStatus.CANCELLED: "🚫 bekor qilindi",
}
ROLE_LABELS = {UserRole.STUDENT: "O‘quvchilar", UserRole.TEACHER: "O‘qituvchilar", UserRole.ADMIN: "Adminlar"}
MAX_IDS = 500


class CmBroadcast(StatesGroup):
    text = State()
    user_ids = State()


def _audience_label(n: Notification, course_title: str | None = None) -> str:
    match n.audience_type:
        case NotificationAudience.ALL:
            return "Hammaga"
        case NotificationAudience.ROLE:
            return ROLE_LABELS.get(UserRole(n.audience["role"]), n.audience["role"])
        case NotificationAudience.COURSE:
            return f"Kurs: {esc(course_title or n.audience.get('course_id'))}"
        case _:
            return f"Tanlangan: {len(n.audience.get('user_ids', []))} ta"


async def _menu_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    items = await notifications.recent_broadcasts(session, limit=8)
    text = (
        "📢 <b>Bildirishnomalar</b>\n\nOxirgi xabarlar:"
        if items
        else "📢 <b>Bildirishnomalar</b>\n\nHali xabar yuborilmagan."
    )
    buttons = [
        btn(
            f"#{n.id} {STATUS_LABELS[n.status]} · {n.sent_count}/{n.total_count} · {truncate(n.body, 18)}",
            CmBcCb(action="view", item_id=n.id),
        )
        for n in items
    ]
    buttons.append(btn("✍️ Yangi xabar", CmBcCb(action="new")))
    return text, rows(*buttons)


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_NOTIFICATIONS)
async def broadcast_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.clear()
    text, kb = await _menu_screen(session)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmBcCb.filter(F.action == "menu"))
async def broadcast_menu_cb(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    require_admin(user, PERM)
    text, kb = await _menu_screen(session)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(CmBcCb.filter(F.action == "new"))
async def broadcast_new(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.set_state(CmBroadcast.text)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Xabar matnini yuboring (oddiy matn, 3500 belgigacha). Bekor qilish: /menu"
        )


@router.message(CmBroadcast.text, F.text)
async def broadcast_text(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    body = (message.text or "").strip()
    if not body or len(body) > 3500:
        raise InvalidInput("Xabar matni 1–3500 belgidan iborat bo‘lishi kerak.")
    await state.update_data(body=body)
    await state.set_state(None)  # keep data, stop waiting for text
    await message.answer(
        "Kimlarga yuborilsin?",
        reply_markup=rows(
            btn("👥 Hammaga", CmBcCb(action="aud", value="all")),
            btn("🎓 O‘quvchilarga", CmBcCb(action="aud", value="role:student")),
            btn("👨‍🏫 O‘qituvchilarga", CmBcCb(action="aud", value="role:teacher")),
            btn("🛠 Adminlarga", CmBcCb(action="aud", value="role:admin")),
            btn("📚 Kurs bo‘yicha", CmBcCb(action="aud", value="course")),
            btn("🆔 Tanlangan foydalanuvchilar", CmBcCb(action="aud", value="users")),
            width=2,
        ),
    )


async def _draft_preview(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    audience_type: NotificationAudience,
    audience: dict,
    course_title: str | None = None,
) -> None:
    data = await state.get_data()
    if "body" not in data:
        raise InvalidInput("Xabar matni topilmadi. Qaytadan boshlang.")
    draft = await notifications.create_broadcast(
        session, user, body=data["body"], audience_type=audience_type, audience=audience
    )
    count = await notifications.count_recipients(session, draft)
    await state.clear()
    await message.answer(
        "📢 <b>Ko‘rib chiqing</b>\n\n"
        f"Kimga: {_audience_label(draft, course_title)}\n"
        f"Qabul qiluvchilar: {count} ta (botni bloklaganlar va e’lonlarni o‘chirganlar hisobga olinmaydi)\n\n"
        f"{draft.body}",
        reply_markup=rows(
            btn("📤 Yuborish", CmBcCb(action="send", item_id=draft.id)),
            btn("❌ Bekor qilish", CmBcCb(action="cancel", item_id=draft.id)),
            width=2,
        ),
    )


@router.callback_query(CmBcCb.filter(F.action == "aud"))
async def broadcast_audience(
    callback: CallbackQuery, callback_data: CmBcCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    value = callback_data.value
    if value == "all":
        await _draft_preview(callback.message, session, user, state, NotificationAudience.ALL, {})
    elif value.startswith("role:"):
        role = UserRole(value.removeprefix("role:"))
        await _draft_preview(
            callback.message, session, user, state, NotificationAudience.ROLE, {"role": role.value}
        )
    elif value == "course":
        courses = (await session.scalars(select(Course).order_by(Course.position, Course.id).limit(40))).all()
        if not courses:
            raise NotFound("Kurslar yo‘q.")
        await callback.message.answer(
            "Kursni tanlang (shu kursni ochgan o‘quvchilarga yuboriladi):",
            reply_markup=rows(*(btn(c.title, CmBcCb(action="course", item_id=c.id)) for c in courses)),
        )
    elif value == "users":
        await state.set_state(CmBroadcast.user_ids)
        await callback.message.answer(
            "Foydalanuvchilarning Telegram ID raqamlarini yuboring "
            "(bo‘sh joy yoki vergul bilan, 500 tagacha)."
        )


@router.callback_query(CmBcCb.filter(F.action == "course"))
async def broadcast_course(
    callback: CallbackQuery, callback_data: CmBcCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    course = await session.get(Course, callback_data.item_id)
    if course is None:
        raise NotFound("Kurs topilmadi.")
    await callback.answer()
    if isinstance(callback.message, Message):
        await _draft_preview(
            callback.message,
            session,
            user,
            state,
            NotificationAudience.COURSE,
            {"course_id": course.id},
            course.title,
        )


@router.message(CmBroadcast.user_ids, F.text)
async def broadcast_user_ids(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    raw = [p for p in re.split(r"[\s,;]+", message.text or "") if p]
    if not raw or len(raw) > MAX_IDS or not all(p.isdigit() for p in raw):
        raise InvalidInput(f"1–{MAX_IDS} ta raqamli Telegram ID kiriting.")
    found = list(
        (await session.scalars(select(User.id).where(User.telegram_id.in_({int(p) for p in raw})))).all()
    )
    if not found:
        raise NotFound("Bu ID lar bilan foydalanuvchilar topilmadi.")
    if len(found) < len(set(raw)):
        await message.answer(f"ℹ️ {len(set(raw)) - len(found)} ta ID topilmadi, ular o‘tkazib yuboriladi.")
    await _draft_preview(message, session, user, state, NotificationAudience.USERS, {"user_ids": found})


@router.callback_query(CmBcCb.filter(F.action == "send"))
async def broadcast_send(
    callback: CallbackQuery, callback_data: CmBcCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    notification = await notifications.confirm_broadcast(session, user, callback_data.item_id)
    await callback.answer("Navbatga qo‘yildi")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=rows(btn("📊 Holatini ko‘rish", CmBcCb(action="view", item_id=notification.id)))
        )


@router.callback_query(CmBcCb.filter(F.action == "cancel"))
async def broadcast_cancel(
    callback: CallbackQuery, callback_data: CmBcCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    await notifications.cancel_broadcast(session, user, callback_data.item_id)
    await callback.answer("Bekor qilindi")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(CmBcCb.filter(F.action == "view"))
async def broadcast_view(
    callback: CallbackQuery, callback_data: CmBcCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    n = await session.get(Notification, callback_data.item_id)
    if n is None or n.kind != notifications.KIND_MANUAL:
        raise NotFound()
    course_title = None
    if n.audience_type == NotificationAudience.COURSE:
        course_title = await session.scalar(
            select(Course.title).where(Course.id == n.audience.get("course_id"))
        )
    text = (
        f"📢 <b>Xabar #{n.id}</b> — {STATUS_LABELS[n.status]}\n\n"
        f"Kimga: {_audience_label(n, course_title)}\n"
        f"Yaratilgan: {fmt_dt(n.created_at)}\n"
        f"Boshlangan: {fmt_dt(n.started_at)} · Tugagan: {fmt_dt(n.finished_at)}\n"
        f"Jami: {n.total_count} · Yuborildi: {n.sent_count} · Xato/bloklagan: {n.failed_count}\n\n"
        f"{n.body}"
    )
    active = n.status in (NotificationStatus.DRAFT, NotificationStatus.QUEUED, NotificationStatus.SENDING)
    kb = rows(
        btn("🔄 Yangilash", CmBcCb(action="view", item_id=n.id)),
        btn("🚫 To‘xtatish", CmBcCb(action="cancel", item_id=n.id)) if active else None,
        btn("⬅️ Orqaga", CmBcCb(action="menu")),
        width=2,
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=kb)
