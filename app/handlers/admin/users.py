"""Admin: users, roles, blocking and admin permissions (TZ §3.1, §15.5)."""

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import BigInteger, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, User, UserRole, UserStatus
from app.keyboards import menus
from app.services.access import AdminFilter, has_permission, is_superadmin, require_admin
from app.services.audit import log_action
from app.services.errors import AccessDenied, NotFound
from app.services.subscriptions import get_active_subscription
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="admin.users")

SEARCH_LIMIT = 10
LIST_LIMIT = 20

PERMISSION_LABELS: dict[AdminPermission, str] = {
    AdminPermission.USERS: "Foydalanuvchilar",
    AdminPermission.ADMINS: "Admin vakolatlari",
    AdminPermission.TEACHERS: "O‘qituvchilar",
    AdminPermission.TEACHER_SESSIONS: "O‘qituvchi sessiyalari",
    AdminPermission.CONTENT: "Kontent",
    AdminPermission.QUIZZES: "Testlar",
    AdminPermission.PLANS: "Tariflar",
    AdminPermission.PAYMENTS: "To‘lovlar",
    AdminPermission.NOTIFICATIONS: "Bildirishnomalar",
    AdminPermission.LIVE: "Jonli darslar",
    AdminPermission.STATS: "Statistika",
    AdminPermission.AUDIT: "Audit jurnali",
    AdminPermission.SETTINGS: "Sozlamalar",
}

ROLE_LABELS = {UserRole.ADMIN: "Administrator", UserRole.TEACHER: "O‘qituvchi", UserRole.STUDENT: "O‘quvchi"}


class CoUserSearch(StatesGroup):
    query = State()


class CoUserCb(CallbackData, prefix="co_usr"):
    action: str  # view | block | unblock | mkadmin | rmadmin | perms | toggle | admins | blocked | search
    user_id: int = 0
    perm: str = ""


def _menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Qidirish", callback_data=CoUserCb(action="search"))
    kb.button(text="🛠 Adminlar", callback_data=CoUserCb(action="admins"))
    kb.button(text="⛔ Bloklanganlar", callback_data=CoUserCb(action="blocked"))
    kb.adjust(1, 2)
    return kb.as_markup()


def _user_line(u: User) -> str:
    username = f" @{u.username}" if u.username else ""
    return truncate(f"{u.full_name}{username} · {u.telegram_id}", 60)


def _list_kb(users: list[User]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in users:
        kb.button(text=_user_line(u), callback_data=CoUserCb(action="view", user_id=u.id))
    kb.adjust(1)
    return kb.as_markup()


async def _get_target(session: AsyncSession, user_id: int) -> User:
    target = await session.get(User, user_id)
    if target is None:
        raise NotFound("Foydalanuvchi topilmadi.")
    return target


def _guard_target(actor: User, target: User) -> None:
    """Nobody manages superadmins or themselves through the bot."""
    if is_superadmin(target):
        raise AccessDenied("Superadmin hisobini bot orqali o‘zgartirib bo‘lmaydi.")
    if actor.id == target.id:
        raise AccessDenied("O‘z hisobingizni o‘zgartira olmaysiz.")


async def _card(session: AsyncSession, actor: User, target: User) -> tuple[str, InlineKeyboardMarkup]:
    sub = await get_active_subscription(session, target.id)
    lines = [
        "<b>👤 Foydalanuvchi</b>",
        f"Ism: {esc(target.full_name)}",
        f"Username: {'@' + esc(target.username) if target.username else '—'}",
        f"Telegram ID: <code>{target.telegram_id}</code>",
        f"Rol: {ROLE_LABELS.get(target.role, target.role)}"
        + (" (superadmin)" if is_superadmin(target) else ""),
        f"Holat: {'⛔ bloklangan' if target.status == UserStatus.BLOCKED else '✅ faol'}",
        f"Obuna: {'faol, ' + fmt_dt(sub.ends_at) + ' gacha' if sub else 'yo‘q'}",
        f"Ro‘yxatdan o‘tgan: {fmt_dt(target.created_at)}",
        f"Oxirgi faollik: {fmt_dt(target.last_seen_at)}",
    ]
    if target.role == UserRole.ADMIN and not is_superadmin(target):
        perms = [PERMISSION_LABELS.get(p, p) for p in target.permissions or []]  # type: ignore[call-overload]
        lines.append(f"Vakolatlar: {esc(', '.join(perms)) if perms else '—'}")

    kb = InlineKeyboardBuilder()
    editable = not is_superadmin(target) and actor.id != target.id
    if editable and has_permission(actor, AdminPermission.USERS):
        if target.status == UserStatus.BLOCKED:
            kb.button(
                text="✅ Blokdan chiqarish", callback_data=CoUserCb(action="unblock", user_id=target.id)
            )
        else:
            kb.button(text="⛔ Bloklash", callback_data=CoUserCb(action="block", user_id=target.id))
    if editable and has_permission(actor, AdminPermission.ADMINS):
        if target.role == UserRole.ADMIN:
            kb.button(text="🔑 Vakolatlar", callback_data=CoUserCb(action="perms", user_id=target.id))
            kb.button(text="⬇️ Adminlikdan olish", callback_data=CoUserCb(action="rmadmin", user_id=target.id))
        elif target.role == UserRole.STUDENT:
            kb.button(text="⬆️ Admin qilish", callback_data=CoUserCb(action="mkadmin", user_id=target.id))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def _show_card(callback: CallbackQuery, session: AsyncSession, actor: User, target: User) -> None:
    text, markup = await _card(session, actor, target)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.message(F.text == menus.BTN_A_USERS, AdminFilter(AdminPermission.USERS))
async def users_menu(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    require_admin(user, AdminPermission.USERS)
    await state.clear()
    total = await session.scalar(select(func.count()).select_from(User))
    blocked = await session.scalar(select(func.count()).where(User.status == UserStatus.BLOCKED))
    await message.answer(
        f"<b>👥 Foydalanuvchilar</b>\n\nJami: {total}\nBloklangan: {blocked}\n\n"
        "Foydalanuvchini Telegram ID, @username yoki ism bo‘yicha qidiring.",
        reply_markup=_menu_kb(),
    )


@router.callback_query(CoUserCb.filter(F.action == "search"))
async def search_start(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, AdminPermission.USERS)
    await state.set_state(CoUserSearch.query)
    if isinstance(callback.message, Message):
        await callback.message.answer("Telegram ID, @username yoki ismni yuboring (bekor qilish: /menu):")
    await callback.answer()


@router.message(CoUserSearch.query, F.text)
async def search_run(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    require_admin(user, AdminPermission.USERS)
    query = (message.text or "").strip().lstrip("@")[:64]
    if not query:
        await message.answer("Qidiruv so‘rovini yuboring.")
        return
    conditions = [
        User.username.ilike(f"%{query}%"),
        User.first_name.ilike(f"%{query}%"),
        User.last_name.ilike(f"%{query}%"),
    ]
    if query.isdigit():
        conditions.append(User.telegram_id == cast(query, BigInteger))
    users = list(
        (
            await session.scalars(
                select(User).where(or_(*conditions)).order_by(User.created_at.desc()).limit(SEARCH_LIMIT)
            )
        ).all()
    )
    await state.clear()
    if not users:
        await message.answer("Hech narsa topilmadi.", reply_markup=_menu_kb())
        return
    await message.answer(f"Topildi: {len(users)}", reply_markup=_list_kb(users))


@router.callback_query(CoUserCb.filter(F.action.in_({"admins", "blocked"})))
async def list_users(
    callback: CallbackQuery, callback_data: CoUserCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.USERS)
    if callback_data.action == "admins":
        stmt, title = select(User).where(User.role == UserRole.ADMIN), "🛠 Adminlar"
    else:
        stmt, title = select(User).where(User.status == UserStatus.BLOCKED), "⛔ Bloklanganlar"
    users = list((await session.scalars(stmt.order_by(User.created_at.desc()).limit(LIST_LIMIT))).all())
    if isinstance(callback.message, Message):
        if users:
            await callback.message.answer(f"<b>{title}</b>", reply_markup=_list_kb(users))
        else:
            await callback.message.answer(f"<b>{title}</b>\n\nRo‘yxat bo‘sh.")
    await callback.answer()


@router.callback_query(CoUserCb.filter(F.action == "view"))
async def view_user(
    callback: CallbackQuery, callback_data: CoUserCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.USERS)
    target = await _get_target(session, callback_data.user_id)
    text, markup = await _card(session, user, target)
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(CoUserCb.filter(F.action.in_({"block", "unblock"})))
async def set_blocked(
    callback: CallbackQuery, callback_data: CoUserCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.USERS)
    target = await _get_target(session, callback_data.user_id)
    _guard_target(user, target)
    new_status = UserStatus.BLOCKED if callback_data.action == "block" else UserStatus.ACTIVE
    if target.status != new_status:
        target.status = new_status
        await log_action(
            session,
            "user.blocked" if new_status == UserStatus.BLOCKED else "user.unblocked",
            actor=user,
            entity_type="user",
            entity_id=target.id,
            details={"telegram_id": target.telegram_id},
        )
    await _show_card(callback, session, user, target)


@router.callback_query(CoUserCb.filter(F.action.in_({"mkadmin", "rmadmin"})))
async def set_admin(
    callback: CallbackQuery, callback_data: CoUserCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.ADMINS)
    target = await _get_target(session, callback_data.user_id)
    _guard_target(user, target)
    if callback_data.action == "mkadmin":
        if target.role != UserRole.STUDENT:
            raise AccessDenied("Faqat o‘quvchini admin qilish mumkin.")
        target.role = UserRole.ADMIN
        target.permissions = []
        action = "user.admin_granted"
    else:
        if target.role != UserRole.ADMIN:
            raise NotFound("Bu foydalanuvchi admin emas.")
        target.role = UserRole.STUDENT
        target.permissions = []
        action = "user.admin_revoked"
    await log_action(session, action, actor=user, entity_type="user", entity_id=target.id)
    await _show_card(callback, session, user, target)


def _perms_kb(actor: User, target: User) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    current = set(target.permissions or [])
    for perm, label in PERMISSION_LABELS.items():
        mark = "✅" if perm.value in current else "▫️"
        # An admin may only grant/revoke permissions they hold themselves.
        if has_permission(actor, perm):
            kb.button(
                text=f"{mark} {label}",
                callback_data=CoUserCb(action="toggle", user_id=target.id, perm=perm.value),
            )
    kb.button(text="⬅️ Orqaga", callback_data=CoUserCb(action="view", user_id=target.id))
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(CoUserCb.filter(F.action == "perms"))
async def edit_perms(
    callback: CallbackQuery, callback_data: CoUserCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.ADMINS)
    target = await _get_target(session, callback_data.user_id)
    _guard_target(user, target)
    if target.role != UserRole.ADMIN:
        raise NotFound("Bu foydalanuvchi admin emas.")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"<b>🔑 Vakolatlar</b>: {esc(target.full_name)}\n\nBosish orqali yoqing/o‘chiring.",
            reply_markup=_perms_kb(user, target),
        )
    await callback.answer()


@router.callback_query(CoUserCb.filter(F.action == "toggle"))
async def toggle_perm(
    callback: CallbackQuery, callback_data: CoUserCb, user: User, session: AsyncSession
) -> None:
    require_admin(user, AdminPermission.ADMINS)
    target = await _get_target(session, callback_data.user_id)
    _guard_target(user, target)
    try:
        perm = AdminPermission(callback_data.perm)
    except ValueError as exc:
        raise NotFound() from exc
    if target.role != UserRole.ADMIN:
        raise NotFound("Bu foydalanuvchi admin emas.")
    if not has_permission(user, perm):
        raise AccessDenied("O‘zingizda yo‘q vakolatni bera olmaysiz.")

    current = set(target.permissions or [])
    granted = perm.value not in current
    current.symmetric_difference_update({perm.value})
    target.permissions = sorted(current)
    await log_action(
        session,
        "user.permission_granted" if granted else "user.permission_revoked",
        actor=user,
        entity_type="user",
        entity_id=target.id,
        details={"permission": perm.value},
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=_perms_kb(user, target))
    await callback.answer("Saqlandi")
