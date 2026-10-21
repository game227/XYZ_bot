"""Admin: 📅 Jonli darslar — create/edit sessions, link visibility, registration, status (TZ §11)."""

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, Course, LiveSession, LiveSessionStatus, User
from app.keyboards import menus
from app.keyboards.commerce import CmAdmLiveCb, btn, rows
from app.services import live, notifications
from app.services.access import AdminFilter, require_admin
from app.services.errors import InvalidInput
from app.utils.text import esc
from app.utils.time import fmt_dt

router = Router(name="admin.live")

PERM = AdminPermission.LIVE
FIELD_LABELS = {
    "title": "Nomi",
    "starts_at": "Boshlanish vaqti",
    "duration_minutes": "Davomiyligi",
    "join_url": "Havola",
    "description": "Tavsif",
    "requirements": "Shartlar",
}
STATUS_ACTIONS = {
    LiveSessionStatus.STARTED: "▶️ Boshlandi",
    LiveSessionStatus.FINISHED: "✅ Yakunlandi",
    LiveSessionStatus.CANCELLED: "🚫 Bekor qilish",
}
TIME_HINT = "Vaqtni Toshkent vaqti bilan KK.OO.YYYY SS:DD ko‘rinishida kiriting, masalan: 25.10.2026 18:30"


class CmLiveCreate(StatesGroup):
    title = State()
    starts_at = State()
    duration = State()
    link = State()
    description = State()


class CmLiveEdit(StatesGroup):
    value = State()


async def _list_screen(session: AsyncSession, past: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    items = await live.list_admin(session, include_past=past)
    title = "📅 <b>Jonli darslar</b>" + (" — barchasi" if past else "")
    text = title + ("\n\nDarslar yo‘q." if not items else "")
    buttons = [
        btn(
            f"{live.STATUS_LABELS[s.status][:2]} {fmt_dt(s.starts_at)} · {s.title}",
            CmAdmLiveCb(action="view", live_id=s.id),
        )
        for s in items
    ]
    buttons.append(btn("➕ Yangi jonli dars", CmAdmLiveCb(action="new")))
    buttons.append(
        btn("⬅️ Faollar" if past else "📜 Barchasi", CmAdmLiveCb(action="list" if past else "past"))
    )
    return text, rows(*buttons)


async def _detail(session: AsyncSession, item: LiveSession) -> tuple[str, InlineKeyboardMarkup]:
    course = (
        await session.scalar(select(Course.title).where(Course.id == item.course_id))
        if item.course_id
        else None
    )
    count = await live.registration_count(session, item.id)
    text = (
        f"📅 <b>{esc(item.title)}</b> — {live.STATUS_LABELS[item.status]}\n\n"
        f"🕒 {fmt_dt(item.starts_at)}"
        + (f" · {item.duration_minutes} daqiqa" if item.duration_minutes else "")
        + f"\n📚 Kurs: {esc(course or '—')}\n"
        f"🔗 Havola: {esc(item.join_url or '—')}\n"
        f"👁 Havola ko‘rinishi: {live.VISIBILITY_LABELS[item.link_visibility]}\n"
        f"📝 Ro‘yxatdan o‘tish: {'yoqilgan' if item.registration_enabled else 'o‘chirilgan'}"
        f" ({count} kishi)\n"
        f"Tavsif: {esc(item.description or '—')}\n"
        f"Shartlar: {esc(item.requirements or '—')}"
    )
    editable = item.status in (LiveSessionStatus.SCHEDULED, LiveSessionStatus.STARTED)
    buttons = []
    if editable:
        buttons += [
            btn(f"✏️ {label}", CmAdmLiveCb(action="edit", live_id=item.id, value=f))
            for f, label in FIELD_LABELS.items()
        ]
        buttons += [
            btn("👁 Ko‘rinishni o‘zgartirish", CmAdmLiveCb(action="vis", live_id=item.id)),
            btn("📝 Ro‘yxat on/off", CmAdmLiveCb(action="reg", live_id=item.id)),
            btn("📚 Kursni tanlash", CmAdmLiveCb(action="course", live_id=item.id)),
        ]
        buttons += [
            btn(label, CmAdmLiveCb(action="status", live_id=item.id, value=status.value))
            for status, label in STATUS_ACTIONS.items()
            if status in live.ALLOWED_TRANSITIONS[item.status]
        ]
    buttons.append(btn("⬅️ Ro‘yxat", CmAdmLiveCb(action="list")))
    return text, rows(*buttons, width=2)


async def _show(callback: CallbackQuery, session: AsyncSession, item: LiveSession) -> None:
    text, kb = await _detail(session, item)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_LIVE)
async def live_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.clear()
    text, kb = await _list_screen(session)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmAdmLiveCb.filter(F.action.in_({"list", "past"})))
async def live_list(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, kb = await _list_screen(session, past=callback_data.action == "past")
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(CmAdmLiveCb.filter(F.action == "view"))
async def live_view(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    await callback.answer()
    await _show(callback, session, await live.get_live(session, callback_data.live_id))


@router.callback_query(CmAdmLiveCb.filter(F.action == "status"))
async def live_status(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    status = LiveSessionStatus(callback_data.value)
    item = await live.set_status(session, user, callback_data.live_id, status)
    if status == LiveSessionStatus.CANCELLED:
        await notifications.notify_live_registrants(
            session, item.id, f"🚫 «{esc(item.title)}» jonli darsi ({fmt_dt(item.starts_at)}) bekor qilindi."
        )
    await callback.answer("Saqlandi")
    await _show(callback, session, item)


@router.callback_query(CmAdmLiveCb.filter(F.action == "reg"))
async def live_toggle_reg(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    item = await live.toggle_registration(session, user, callback_data.live_id)
    await callback.answer("Saqlandi")
    await _show(callback, session, item)


@router.callback_query(CmAdmLiveCb.filter(F.action == "vis"))
async def live_visibility(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    item = await live.cycle_visibility(session, user, callback_data.live_id)
    await callback.answer(live.VISIBILITY_LABELS[item.link_visibility])
    await _show(callback, session, item)


@router.callback_query(CmAdmLiveCb.filter(F.action == "course"))
async def live_course_pick(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    courses = (await session.scalars(select(Course).order_by(Course.position, Course.id).limit(40))).all()
    await callback.answer()
    buttons = [
        btn(c.title, CmAdmLiveCb(action="setcourse", live_id=callback_data.live_id, value=str(c.id)))
        for c in courses
    ]
    buttons.append(
        btn("— Kurssiz", CmAdmLiveCb(action="setcourse", live_id=callback_data.live_id, value="0"))
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=rows(*buttons))


@router.callback_query(CmAdmLiveCb.filter(F.action == "setcourse"))
async def live_course_set(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    item = await live.set_course(session, user, callback_data.live_id, int(callback_data.value) or None)
    await callback.answer("Saqlandi")
    await _show(callback, session, item)


# --- create -------------------------------------------------------------------------------


@router.callback_query(CmAdmLiveCb.filter(F.action == "new"))
async def live_new(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.set_state(CmLiveCreate.title)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Jonli dars nomini kiriting. Bekor qilish: /menu")


@router.message(CmLiveCreate.title, F.text)
async def live_new_title(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    title = (message.text or "").strip()
    if not title or len(title) > 255:
        raise InvalidInput("Nom 1–255 belgi bo‘lishi kerak.")
    await state.update_data(title=title)
    await state.set_state(CmLiveCreate.starts_at)
    await message.answer(TIME_HINT)


@router.message(CmLiveCreate.starts_at, F.text)
async def live_new_starts(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    starts_at = live.parse_local_datetime(message.text or "")
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(CmLiveCreate.duration)
    await message.answer("Davomiyligi necha daqiqa? («-» — ko‘rsatilmasin)")


@router.message(CmLiveCreate.duration, F.text)
async def live_new_duration(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    raw = (message.text or "").strip()
    await state.update_data(duration=None if raw == "-" else live.parse_minutes(raw))
    await state.set_state(CmLiveCreate.link)
    await message.answer("Ulanish havolasini yuboring (Zoom, Meet, Telegram...). «-» — keyinroq qo‘shiladi.")


@router.message(CmLiveCreate.link, F.text)
async def live_new_link(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    raw = (message.text or "").strip()
    await state.update_data(link=None if raw == "-" else live.validate_url(raw))
    await state.set_state(CmLiveCreate.description)
    await message.answer("Qisqa tavsif yozing yoki «-» yuboring:")


@router.message(CmLiveCreate.description, F.text)
async def live_new_description(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    raw = (message.text or "").strip()
    if len(raw) > 2000:
        raise InvalidInput("Tavsif 2000 belgidan oshmasligi kerak.")
    data = await state.get_data()
    item = await live.create_live(
        session,
        user,
        live.LiveDraft(
            title=data["title"],
            starts_at=datetime.fromisoformat(data["starts_at"]),
            duration_minutes=data.get("duration"),
            join_url=data.get("link"),
            description=None if raw == "-" else raw,
        ),
    )
    await state.clear()
    text, kb = await _detail(session, item)
    await message.answer(
        "✅ Jonli dars yaratildi. Havola ko‘rinishi va ro‘yxatdan o‘tishni quyida sozlang.\n\n" + text,
        reply_markup=kb,
    )


# --- edit ---------------------------------------------------------------------------------


@router.callback_query(CmAdmLiveCb.filter(F.action == "edit"))
async def live_edit(
    callback: CallbackQuery, callback_data: CmAdmLiveCb, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    if callback_data.value not in live.LIVE_FIELDS:
        raise InvalidInput()
    await state.set_state(CmLiveEdit.value)
    await state.update_data(live_id=callback_data.live_id, field=callback_data.value)
    hint = TIME_HINT if callback_data.value == "starts_at" else "Yangi qiymatni yuboring («-» — tozalash)."
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(f"{FIELD_LABELS[callback_data.value]}: {hint}\nBekor qilish: /menu")


@router.message(CmLiveEdit.value, F.text)
async def live_edit_value(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    data = await state.get_data()
    item = await live.update_field(session, user, int(data["live_id"]), data["field"], message.text or "")
    await state.clear()
    text, kb = await _detail(session, item)
    await message.answer("✅ Saqlandi.\n\n" + text, reply_markup=kb)
