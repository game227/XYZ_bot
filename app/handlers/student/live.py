"""Student: 📅 Jonli darslar — upcoming sessions, registration, join link for permitted users (TZ §11)."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Course, LiveLinkVisibility, LiveSession, LiveSessionStatus, User
from app.keyboards import menus
from app.keyboards.commerce import CmLiveCb, CmPlanCb, btn, rows
from app.services import live
from app.utils.text import esc
from app.utils.time import fmt_dt

router = Router(name="student.live")


async def _list_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await live.list_upcoming(session)
    if not items:
        return "📅 <b>Jonli darslar</b>\n\nYaqin kunlarda jonli darslar rejalashtirilmagan.", None
    buttons = [
        btn(
            f"{'🔴 ' if s.status == LiveSessionStatus.STARTED else ''}{fmt_dt(s.starts_at)} · {s.title}",
            CmLiveCb(action="view", live_id=s.id),
        )
        for s in items
    ]
    return "📅 <b>Jonli darslar</b>\n\nDarsni tanlang:", rows(*buttons)


async def _detail(session: AsyncSession, user: User, item: LiveSession) -> tuple[str, InlineKeyboardMarkup]:
    registered = await live.is_registered(session, item.id, user.id)
    show_link = await live.can_see_link(session, user, item)
    course = (
        await session.scalar(select(Course.title).where(Course.id == item.course_id))
        if item.course_id
        else None
    )
    lines = [
        f"📅 <b>{esc(item.title)}</b>",
        live.STATUS_LABELS[item.status],
        "",
        f"🕒 {fmt_dt(item.starts_at)}",
    ]
    if item.duration_minutes:
        lines.append(f"⏱ {item.duration_minutes} daqiqa")
    if course:
        lines.append(f"📚 Kurs: {esc(course)}")
    if item.description:
        lines += ["", esc(item.description)]
    if item.requirements:
        lines += ["", f"<b>Ishtirok etish shartlari:</b> {esc(item.requirements)}"]
    if item.registration_enabled:
        lines += ["", "✅ Siz ro‘yxatdan o‘tgansiz." if registered else "Qatnashish uchun ro‘yxatdan o‘ting."]

    buttons: list[InlineKeyboardButton | None] = []
    if show_link and item.join_url:
        buttons.append(InlineKeyboardButton(text="🔗 Darsga qo‘shilish", url=item.join_url))
    elif item.join_url:
        if item.link_visibility == LiveLinkVisibility.REGISTERED and not registered:
            lines += ["", "🔒 Havola ro‘yxatdan o‘tgan obunachilarga ko‘rinadi."]
        else:
            lines += ["", "🔒 Havola faqat faol obunachilarga ko‘rinadi."]
            buttons.append(btn("💳 Obuna olish", CmPlanCb(action="list")))
    else:
        lines += ["", "Havola dars oldidan e’lon qilinadi."]
    if item.registration_enabled and item.status == LiveSessionStatus.SCHEDULED:
        buttons.append(
            btn("❌ Ro‘yxatdan chiqish", CmLiveCb(action="unreg", live_id=item.id))
            if registered
            else btn("📝 Ro‘yxatdan o‘tish", CmLiveCb(action="reg", live_id=item.id))
        )
    buttons.append(btn("⬅️ Ro‘yxat", CmLiveCb(action="list")))
    return "\n".join(lines), rows(*buttons)


@router.message(F.text == menus.BTN_LIVE)
async def live_menu(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _list_screen(session)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmLiveCb.filter(F.action == "list"))
async def live_list(callback: CallbackQuery, session: AsyncSession) -> None:
    text, kb = await _list_screen(session)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(CmLiveCb.filter(F.action == "view"))
async def live_view(
    callback: CallbackQuery, callback_data: CmLiveCb, session: AsyncSession, user: User
) -> None:
    item = await live.get_live(session, callback_data.live_id)
    text, kb = await _detail(session, user, item)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(CmLiveCb.filter(F.action.in_({"reg", "unreg"})))
async def live_register(
    callback: CallbackQuery, callback_data: CmLiveCb, session: AsyncSession, user: User
) -> None:
    if callback_data.action == "reg":
        await live.register(session, user, callback_data.live_id)
        await callback.answer("✅ Ro‘yxatdan o‘tdingiz. Dars oldidan eslatma keladi.")
    else:
        await live.unregister(session, user, callback_data.live_id)
        await callback.answer("Ro‘yxatdan chiqdingiz.")
    await session.flush()
    item = await live.get_live(session, callback_data.live_id)
    text, kb = await _detail(session, user, item)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)
