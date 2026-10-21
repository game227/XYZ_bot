"""Student: 💳 Obuna olish and /subscription (TZ §6.2, §6.4).

Flow: plan list → plan details + Telegram ID + payment instructions → proof (photo / document /
text) sent right here in the bot → payment request for the admins' «Obuna so‘rovlari».
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.admin.payments import notify_admins_new_request
from app.keyboards import menus
from app.keyboards.commerce import CmPlanCb, CmReqCb, btn, rows
from app.services import bot_settings, subscriptions
from app.services.errors import Conflict, InvalidInput
from app.utils.text import esc
from app.utils.time import fmt_dt

router = Router(name="student.subscription")


class CmPurchase(StatesGroup):
    waiting_proof = State()


async def _plans_screen(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    view = await subscriptions.subscription_view(session, user.id)
    lines = ["💳 <b>Obuna olish</b>", ""]
    if view.active:
        lines.append(
            f"✅ Obunangiz faol: {fmt_dt(view.ends_at)} gacha. Yangi tarif joriy muddat oxiriga qo‘shiladi."
        )
    else:
        lines.append("Obuna pullik darslar, ularning testlari va video darslarni ochadi.")
    lines.append(f"Telegram ID raqamingiz: <code>{user.telegram_id}</code>")

    pending = await subscriptions.pending_request_for(session, user.id)
    if pending is not None:
        lines += ["", f"⏳ #{pending.id} so‘rovingiz administrator tomonidan ko‘rib chiqilmoqda."]
        return "\n".join(lines), rows(
            btn("🚫 So‘rovni bekor qilish", CmReqCb(action="cancel", request_id=pending.id))
        )

    plans = await subscriptions.list_plans(session)
    if not plans:
        lines += ["", "Hozircha faol tariflar yo‘q. Keyinroq qayta urinib ko‘ring."]
        return "\n".join(lines), None
    lines += ["", "Tarifni tanlang:"]
    buttons = [
        btn(
            f"{plan.name} — {subscriptions.format_price(plan.price, plan.currency)}"
            f" / {plan.duration_days} kun",
            CmPlanCb(action="view", plan_id=plan.id),
        )
        for plan in plans
    ]
    return "\n".join(lines), rows(*buttons)


@router.message(F.text == menus.BTN_SUBSCRIBE)
async def subscribe_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _plans_screen(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmPlanCb.filter(F.action == "list"))
async def plans_list(callback: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _plans_screen(session, user)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(CmPlanCb.filter(F.action == "view"))
async def plan_view(
    callback: CallbackQuery, callback_data: CmPlanCb, session: AsyncSession, user: User
) -> None:
    plan = await subscriptions.get_plan(session, callback_data.plan_id)
    if not plan.is_active:
        raise InvalidInput("Bu tarif hozir mavjud emas.")
    instructions = await bot_settings.get_setting(session, bot_settings.PAYMENT_INSTRUCTIONS)
    lines = [
        f"💳 <b>{esc(plan.name)}</b>",
        f"Narxi: {subscriptions.format_price(plan.price, plan.currency)}",
        f"Muddati: {plan.duration_days} kun",
    ]
    if plan.description:
        lines += ["", esc(plan.description)]
    if plan.terms:
        lines += ["", f"<i>Shartlar:</i> {esc(plan.terms)}"]
    lines += [
        "",
        f"🆔 Telegram ID raqamingiz: <code>{user.telegram_id}</code>",
        "",
        "<b>To‘lov tartibi:</b>",
        esc(instructions),
        "",
        "To‘lovdan so‘ng «🧾 Chek yuborish» tugmasini bosing va chekni shu yerga yuboring.",
    ]
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=rows(
                btn("🧾 Chek yuborish", CmPlanCb(action="buy", plan_id=plan.id)),
                btn("⬅️ Tariflar", CmPlanCb(action="list")),
            ),
        )


@router.callback_query(CmPlanCb.filter(F.action == "buy"))
async def plan_buy(
    callback: CallbackQuery, callback_data: CmPlanCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    plan = await subscriptions.get_plan(session, callback_data.plan_id)
    if not plan.is_active:
        raise InvalidInput("Bu tarif hozir mavjud emas.")
    if await subscriptions.pending_request_for(session, user.id) is not None:
        raise Conflict("Sizda ko‘rib chiqilayotgan so‘rov bor. Administrator javobini kuting.")
    await state.set_state(CmPurchase.waiting_proof)
    await state.update_data(plan_id=plan.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"🧾 <b>{esc(plan.name)}</b> uchun to‘lov chekini yuboring:\n"
            "• chek rasmi (skrinshot) yoki fayl (PDF), izohni rasm ostiga yozishingiz mumkin;\n"
            "• yoki to‘lov ma’lumotini matn bilan yozing (sana, summa, karta oxirgi raqamlari).\n\n"
            "Bekor qilish: /menu"
        )


async def _submit(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    proof_kind: str,
    file_id: str | None,
    comment: str | None,
) -> None:
    data = await state.get_data()
    request = await subscriptions.create_payment_request(
        session,
        user,
        int(data["plan_id"]),
        proof_kind=proof_kind,
        proof_file_id=file_id,
        user_comment=comment,
    )
    await state.clear()
    await session.commit()
    await message.answer(
        f"✅ So‘rovingiz #{request.id} qabul qilindi.\n"
        "Administrator to‘lovni tekshirgach, obunangiz faollashadi va sizga xabar keladi.",
        reply_markup=menus.student_menu(),
    )
    await notify_admins_new_request(bot, session, request)


@router.message(CmPurchase.waiting_proof, F.photo)
async def proof_photo(
    message: Message, bot: Bot, session: AsyncSession, user: User, state: FSMContext
) -> None:
    assert message.photo
    await _submit(
        message,
        bot,
        session,
        user,
        state,
        proof_kind="photo",
        file_id=message.photo[-1].file_id,
        comment=message.caption,
    )


@router.message(CmPurchase.waiting_proof, F.document)
async def proof_document(
    message: Message, bot: Bot, session: AsyncSession, user: User, state: FSMContext
) -> None:
    assert message.document
    if (message.document.file_size or 0) > 20 * 1024 * 1024:
        raise InvalidInput("Fayl juda katta (20 MB gacha).")
    await _submit(
        message,
        bot,
        session,
        user,
        state,
        proof_kind="document",
        file_id=message.document.file_id,
        comment=message.caption,
    )


@router.message(CmPurchase.waiting_proof, F.text)
async def proof_text(
    message: Message, bot: Bot, session: AsyncSession, user: User, state: FSMContext
) -> None:
    assert message.text is not None
    if len(message.text.strip()) < 5:
        raise InvalidInput("To‘lov ma’lumotini batafsilroq yozing yoki chek rasmini yuboring.")
    await _submit(message, bot, session, user, state, proof_kind="text", file_id=None, comment=message.text)


@router.message(CmPurchase.waiting_proof)
async def proof_other(message: Message) -> None:
    await message.answer("Chek rasmini, faylini yoki matnli ma’lumot yuboring. Bekor qilish: /menu")


@router.callback_query(CmReqCb.filter(F.action == "cancel"))
async def request_cancel(
    callback: CallbackQuery, callback_data: CmReqCb, session: AsyncSession, user: User
) -> None:
    await subscriptions.cancel_payment_request(session, user, callback_data.request_id)
    await callback.answer("So‘rov bekor qilindi.")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=rows(btn("💳 Tariflar", CmPlanCb(action="list")))
        )


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, session: AsyncSession, user: User) -> None:
    view = await subscriptions.subscription_view(session, user.id)
    if view.active:
        assert view.ends_at is not None and view.remaining is not None
        text = (
            "💳 <b>Obuna holati</b>\n\n"
            "Holat: ✅ faol\n"
            f"Tarif: {esc(view.plan_name or '—')}\n"
            f"Boshlangan: {fmt_dt(view.starts_at)}\n"
            f"Tugaydi: {fmt_dt(view.ends_at)}\n"
            f"Qolgan vaqt: {subscriptions.format_remaining(view.remaining)}"
        )
        button = "🔄 Obunani uzaytirish"
    else:
        text = "💳 <b>Obuna holati</b>\n\nHolat: ❌ faol emas"
        if view.last_ended_at:
            text += f"\nOxirgi obuna tugagan: {fmt_dt(view.last_ended_at)}"
        text += "\n\nPullik darslar va testlar uchun obuna oling."
        button = "💳 Obuna olish"
    await message.answer(text, reply_markup=rows(btn(button, CmPlanCb(action="list"))))
