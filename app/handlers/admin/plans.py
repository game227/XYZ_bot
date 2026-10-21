"""Admin: 💳 Tariflar va obunalar — plan CRUD and manual subscription grants (TZ §6.1)."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, SubscriptionPlan, User
from app.keyboards import menus
from app.keyboards.commerce import CmAdmPlanCb, CmGrantCb, btn, rows
from app.services import subscriptions
from app.services.access import AdminFilter, has_permission, require_admin
from app.services.errors import AccessDenied, InvalidInput, NotFound
from app.utils.text import esc
from app.utils.time import fmt_dt

router = Router(name="admin.plans")

PERM = AdminPermission.PLANS
FIELD_LABELS = {
    "name": "Nomi",
    "price": "Narxi (so‘m)",
    "duration_days": "Muddati (kun)",
    "description": "Tavsifi",
    "terms": "Foydalanish shartlari",
}


class CmPlanCreate(StatesGroup):
    name = State()
    price = State()
    days = State()
    description = State()


class CmPlanEdit(StatesGroup):
    value = State()


class CmGrant(StatesGroup):
    telegram_id = State()
    days = State()


def _require_grant(user: User) -> None:
    # Granting/cancelling subscriptions is a payments matter.
    if not has_permission(user, AdminPermission.PAYMENTS):
        raise AccessDenied("Bu amal uchun «payments» vakolati kerak.")


async def _plans_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    plans = await subscriptions.list_plans(session, active_only=False)
    text = "💳 <b>Tariflar va obunalar</b>\n\n" + (
        "\n".join(
            f"{'🟢' if p.is_active else '⚪️'} {esc(p.name)} — "
            f"{subscriptions.format_price(p.price, p.currency)}"
            f" / {p.duration_days} kun"
            for p in plans
        )
        or "Tariflar hali yo‘q."
    )
    buttons = [
        btn(f"{'🟢' if p.is_active else '⚪️'} {p.name}", CmAdmPlanCb(action="view", plan_id=p.id))
        for p in plans
    ]
    buttons += [
        btn("➕ Yangi tarif", CmAdmPlanCb(action="new")),
        btn("🎁 Foydalanuvchi obunasi", CmAdmPlanCb(action="grant")),
    ]
    return text, rows(*buttons)


def _plan_text(plan: SubscriptionPlan) -> str:
    return (
        f"💳 <b>{esc(plan.name)}</b> {'🟢 faol' if plan.is_active else '⚪️ nofaol'}\n\n"
        f"Narxi: {subscriptions.format_price(plan.price, plan.currency)}\n"
        f"Muddati: {plan.duration_days} kun\n"
        f"Tavsif: {esc(plan.description or '—')}\n"
        f"Shartlar: {esc(plan.terms or '—')}"
    )


def _plan_kb(plan: SubscriptionPlan) -> InlineKeyboardMarkup:
    buttons = [
        btn(f"✏️ {label}", CmAdmPlanCb(action="edit", plan_id=plan.id, field=f))
        for f, label in FIELD_LABELS.items()
    ]
    buttons += [
        btn(
            "⚪️ Nofaol qilish" if plan.is_active else "🟢 Faollashtirish",
            CmAdmPlanCb(action="toggle", plan_id=plan.id),
        ),
        btn("⬅️ Tariflar", CmAdmPlanCb(action="list")),
    ]
    return rows(*buttons, width=2)


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_PLANS)
async def plans_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.clear()
    text, kb = await _plans_screen(session)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmAdmPlanCb.filter(F.action == "list"))
async def plans_list(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    require_admin(user, PERM)
    text, kb = await _plans_screen(session)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(CmAdmPlanCb.filter(F.action == "view"))
async def plan_view(
    callback: CallbackQuery, callback_data: CmAdmPlanCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    plan = await subscriptions.get_plan(session, callback_data.plan_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(_plan_text(plan), reply_markup=_plan_kb(plan))


@router.callback_query(CmAdmPlanCb.filter(F.action == "toggle"))
async def plan_toggle(
    callback: CallbackQuery, callback_data: CmAdmPlanCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    plan = await subscriptions.toggle_plan(session, user, callback_data.plan_id)
    await callback.answer("Saqlandi")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(_plan_text(plan), reply_markup=_plan_kb(plan))


# --- create -------------------------------------------------------------------------------


@router.callback_query(CmAdmPlanCb.filter(F.action == "new"))
async def plan_new(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.set_state(CmPlanCreate.name)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Yangi tarif nomini kiriting (masalan: «1 oylik»). Bekor qilish: /menu")


@router.message(CmPlanCreate.name, F.text)
async def plan_new_name(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    name = subscriptions.clean_text(message.text, 255, required=True)
    await state.update_data(name=name)
    await state.set_state(CmPlanCreate.price)
    await message.answer("Narxini so‘mda kiriting (masalan: 150000):")


@router.message(CmPlanCreate.price, F.text)
async def plan_new_price(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    price = subscriptions.parse_price(message.text or "")
    await state.update_data(price=str(price))
    await state.set_state(CmPlanCreate.days)
    await message.answer("Amal qilish muddatini kunlarda kiriting (masalan: 30):")


@router.message(CmPlanCreate.days, F.text)
async def plan_new_days(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.update_data(days=subscriptions.parse_days(message.text or ""))
    await state.set_state(CmPlanCreate.description)
    await message.answer("Qisqa tavsif yozing yoki «-» yuboring:")


@router.message(CmPlanCreate.description, F.text)
async def plan_new_description(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    data = await state.get_data()
    description = None if (message.text or "").strip() == "-" else message.text
    plan = await subscriptions.create_plan(
        session,
        user,
        name=data["name"],
        price=subscriptions.parse_price(data["price"]),
        duration_days=int(data["days"]),
        description=description,
    )
    await state.clear()
    await message.answer("✅ Tarif yaratildi.\n\n" + _plan_text(plan), reply_markup=_plan_kb(plan))


# --- edit ---------------------------------------------------------------------------------


@router.callback_query(CmAdmPlanCb.filter(F.action == "edit"))
async def plan_edit(
    callback: CallbackQuery, callback_data: CmAdmPlanCb, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    if callback_data.field not in subscriptions.PLAN_FIELDS:
        raise InvalidInput()
    await state.set_state(CmPlanEdit.value)
    await state.update_data(plan_id=callback_data.plan_id, field=callback_data.field)
    await callback.answer()
    hint = " («-» — tozalash)" if callback_data.field in ("description", "terms") else ""
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Yangi qiymat: {FIELD_LABELS[callback_data.field]}{hint}. Bekor qilish: /menu"
        )


@router.message(CmPlanEdit.value, F.text)
async def plan_edit_value(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    data = await state.get_data()
    plan = await subscriptions.update_plan(
        session, user, int(data["plan_id"]), data["field"], message.text or ""
    )
    await state.clear()
    await message.answer("✅ Saqlandi.\n\n" + _plan_text(plan), reply_markup=_plan_kb(plan))


# --- manual grants ------------------------------------------------------------------------


async def _user_sub_screen(session: AsyncSession, target: User) -> tuple[str, InlineKeyboardMarkup]:
    view = await subscriptions.subscription_view(session, target.id)
    status = f"✅ faol, {fmt_dt(view.ends_at)} gacha" if view.active else "❌ faol emas"
    text = (
        f"👤 {esc(target.full_name)} (ID <code>{target.telegram_id}</code>)\n"
        f"Obuna: {status}\n\n"
        "Obuna berish yoki uzaytirish (joriy muddat oxiriga qo‘shiladi):"
    )
    kb = rows(
        btn("+30 kun", CmGrantCb(action="days", user_id=target.id, days=30)),
        btn("+90 kun", CmGrantCb(action="days", user_id=target.id, days=90)),
        btn("✍️ Boshqa muddat", CmGrantCb(action="custom", user_id=target.id)),
        btn("🚫 Obunani bekor qilish", CmGrantCb(action="cancelask", user_id=target.id))
        if view.active
        else None,
        width=2,
    )
    return text, kb


@router.callback_query(CmAdmPlanCb.filter(F.action == "grant"))
async def grant_start(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    await state.set_state(CmGrant.telegram_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Foydalanuvchining Telegram ID raqamini kiriting. Bekor qilish: /menu")


@router.message(CmGrant.telegram_id, F.text)
async def grant_user(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        raise InvalidInput("Telegram ID faqat raqamlardan iborat.") from None
    target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if target is None:
        raise NotFound("Bu ID bilan foydalanuvchi topilmadi (u botni ishga tushirgan bo‘lishi kerak).")
    await state.clear()
    text, kb = await _user_sub_screen(session, target)
    await message.answer(text, reply_markup=kb)


async def _grant(session: AsyncSession, admin: User, user_id: int, days: int) -> str:
    target = await session.get(User, user_id)
    if target is None:
        raise NotFound()
    subscription = await subscriptions.grant_subscription(session, admin, target.id, days)
    period = f"{fmt_dt(subscription.starts_at)} — {fmt_dt(subscription.ends_at)}"
    return f"✅ {esc(target.full_name)} uchun obuna: {period}"


@router.callback_query(CmGrantCb.filter(F.action == "days"))
async def grant_days(
    callback: CallbackQuery, callback_data: CmGrantCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    text = await _grant(
        session, user, callback_data.user_id, subscriptions.parse_days(str(callback_data.days))
    )
    await session.commit()
    await callback.answer("Saqlandi")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(text)


@router.callback_query(CmGrantCb.filter(F.action == "custom"))
async def grant_custom(
    callback: CallbackQuery, callback_data: CmGrantCb, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    await state.set_state(CmGrant.days)
    await state.update_data(user_id=callback_data.user_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Necha kun qo‘shilsin? (1–3660)")


@router.message(CmGrant.days, F.text)
async def grant_custom_days(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    days = subscriptions.parse_days(message.text or "")
    data = await state.get_data()
    await state.clear()
    await message.answer(await _grant(session, user, int(data["user_id"]), days))


@router.callback_query(CmGrantCb.filter(F.action == "cancelask"))
async def grant_cancel_ask(callback: CallbackQuery, callback_data: CmGrantCb, user: User) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=rows(
                btn("✅ Ha, bekor qilinsin", CmGrantCb(action="cancelsub", user_id=callback_data.user_id)),
                width=1,
            )
        )


@router.callback_query(CmGrantCb.filter(F.action == "cancelsub"))
async def grant_cancel(
    callback: CallbackQuery, callback_data: CmGrantCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    _require_grant(user)
    count = await subscriptions.cancel_subscriptions(session, user, callback_data.user_id)
    await callback.answer("Bekor qilindi" if count else "Faol obuna yo‘q", show_alert=not count)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        if count:
            await callback.message.answer(
                f"🚫 {count} ta obuna bekor qilindi. Pullik kontentga kirish yopildi."
            )
