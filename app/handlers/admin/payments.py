"""Admin: 🧾 Obuna so‘rovlari — review payment proofs, approve (idempotent) or reject (TZ §6.2-6.3)."""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, PaymentRequest, PaymentRequestStatus, SubscriptionPlan, User
from app.keyboards import menus
from app.keyboards.commerce import CmPayCb, btn, pager, rows
from app.services import subscriptions
from app.services.access import AdminFilter, admin_telegram_ids, require_admin
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

logger = logging.getLogger(__name__)

router = Router(name="admin.payments")

PER_PAGE = 10
STATUS_LABELS = {
    PaymentRequestStatus.PENDING: "⏳ Kutilmoqda",
    PaymentRequestStatus.APPROVED: "✅ Tasdiqlangan",
    PaymentRequestStatus.REJECTED: "❌ Rad etilgan",
    PaymentRequestStatus.CANCELLED: "🚫 Bekor qilingan",
}
PERM = AdminPermission.PAYMENTS


class CmPaymentReject(StatesGroup):
    reason = State()


async def request_card(session: AsyncSession, request: PaymentRequest) -> str:
    student = await session.get(User, request.user_id)
    plan = await session.get(SubscriptionPlan, request.plan_id)
    assert student is not None
    username = f" (@{esc(student.username)})" if student.username else ""
    lines = [
        f"🧾 <b>Obuna so‘rovi #{request.id}</b> — {STATUS_LABELS[request.status]}",
        "",
        f"👤 {esc(student.full_name)}{username}",
        f"Telegram ID: <code>{student.telegram_id}</code>",
        f"Tarif: {esc(plan.name if plan else '—')}",
        f"Summa: {subscriptions.format_price(request.amount, request.currency)}",
        f"Muddat: {request.duration_days} kun",
        f"Yuborilgan: {fmt_dt(request.created_at)}",
    ]
    if request.user_comment:
        lines.append(f"Izoh: {esc(truncate(request.user_comment, 700))}")
    if request.status != PaymentRequestStatus.PENDING:
        lines.append(f"Qaror vaqti: {fmt_dt(request.decided_at)}")
        if request.decision_comment:
            lines.append(f"Qaror izohi: {esc(request.decision_comment)}")
    return "\n".join(lines)


def decision_kb(request: PaymentRequest) -> InlineKeyboardMarkup | None:
    if request.status != PaymentRequestStatus.PENDING:
        return None
    return rows(
        btn("✅ Tasdiqlash", CmPayCb(action="approve", request_id=request.id)),
        btn("❌ Rad etish", CmPayCb(action="reject", request_id=request.id)),
        width=2,
    )


async def send_request_card(bot: Bot, session: AsyncSession, chat_id: int, request: PaymentRequest) -> None:
    """Card with the payment proof attached (photo/document re-sent by file_id)."""
    text = await request_card(session, request)
    markup = decision_kb(request)
    if request.proof_kind == "photo" and request.proof_file_id:
        await bot.send_photo(
            chat_id, request.proof_file_id, caption=truncate(text, 1024), reply_markup=markup
        )
    elif request.proof_kind == "document" and request.proof_file_id:
        await bot.send_document(
            chat_id, request.proof_file_id, caption=truncate(text, 1024), reply_markup=markup
        )
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)


async def notify_admins_new_request(bot: Bot, session: AsyncSession, request: PaymentRequest) -> None:
    """Call after commit (no locks held while talking to Telegram)."""
    for chat_id in await admin_telegram_ids(session, PERM):
        try:
            await send_request_card(bot, session, chat_id, request)
        except TelegramAPIError:
            logger.warning("could not notify admin %s about payment request %s", chat_id, request.id)


async def _list_screen(
    session: AsyncSession, status: PaymentRequestStatus, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    items, total = await subscriptions.list_requests(session, status, limit=PER_PAGE, offset=page * PER_PAGE)
    title = "🧾 <b>Obuna so‘rovlari</b>" + ("" if status == PaymentRequestStatus.PENDING else " — tarix")
    text = f"{title}\n\n{STATUS_LABELS[status]}: {total} ta"
    buttons = []
    for item in items:
        student = await session.get(User, item.user_id)
        name = student.full_name if student else "?"
        label = (
            f"#{item.id} · {truncate(name, 20)} · {subscriptions.format_price(item.amount, item.currency)}"
        )
        buttons.append(
            [btn(label, CmPayCb(action="view", request_id=item.id, page=page, status=status.value))]
        )
    if not items:
        text += "\n\nHozircha so‘rovlar yo‘q."
    nav = pager(CmPayCb, page, total, PER_PAGE, action="list", status=status.value)
    if nav:
        buttons.append(nav)
    other = (
        PaymentRequestStatus.APPROVED
        if status == PaymentRequestStatus.PENDING
        else PaymentRequestStatus.PENDING
    )
    other_label = "📜 Tasdiqlanganlar" if other == PaymentRequestStatus.APPROVED else "⏳ Kutilayotganlar"
    switch = [btn(other_label, CmPayCb(action="list", status=other.value))]
    if status == PaymentRequestStatus.APPROVED:
        switch.append(
            btn("❌ Rad etilganlar", CmPayCb(action="list", status=PaymentRequestStatus.REJECTED.value))
        )
    buttons.append(switch)
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_PAYMENT_REQUESTS)
async def payments_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.clear()
    text, kb = await _list_screen(session, PaymentRequestStatus.PENDING, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CmPayCb.filter(F.action == "list"))
async def payments_list(
    callback: CallbackQuery, callback_data: CmPayCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, kb = await _list_screen(session, PaymentRequestStatus(callback_data.status), callback_data.page)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(CmPayCb.filter(F.action == "view"))
async def payment_view(
    callback: CallbackQuery, callback_data: CmPayCb, session: AsyncSession, user: User, bot: Bot
) -> None:
    require_admin(user, PERM)
    request = await subscriptions.get_request(session, callback_data.request_id)
    await callback.answer()
    await send_request_card(bot, session, callback.from_user.id, request)


@router.callback_query(CmPayCb.filter(F.action == "approve"))
async def payment_approve(
    callback: CallbackQuery, callback_data: CmPayCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    subscription = await subscriptions.approve_payment_request(session, user, callback_data.request_id)
    await session.commit()  # release the user row lock before talking to Telegram
    await callback.answer("✅ Tasdiqlandi")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(
            f"✅ So‘rov #{callback_data.request_id} tasdiqlandi.\n"
            f"Obuna: {fmt_dt(subscription.starts_at)} — {fmt_dt(subscription.ends_at)}"
        )


@router.callback_query(CmPayCb.filter(F.action == "reject"))
async def payment_reject_start(
    callback: CallbackQuery, callback_data: CmPayCb, session: AsyncSession, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    request = await subscriptions.get_request(session, callback_data.request_id)
    if request.status != PaymentRequestStatus.PENDING:
        await callback.answer("Bu so‘rov allaqachon ko‘rib chiqilgan.", show_alert=True)
        return
    await state.set_state(CmPaymentReject.reason)
    await state.update_data(request_id=request.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.reply(
            f"#{request.id} so‘rovni rad etish sababini yozing.\n"
            "Sababsiz rad etish uchun «-» yuboring. /menu — bekor qilish."
        )


@router.message(CmPaymentReject.reason, F.text)
async def payment_reject_finish(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    require_admin(user, PERM)
    assert message.text is not None
    data = await state.get_data()
    await state.clear()
    reason = None if message.text.strip() == "-" else message.text
    request = await subscriptions.reject_payment_request(
        session, user, int(data["request_id"]), reason=reason
    )
    await message.answer(f"❌ So‘rov #{request.id} rad etildi. O‘quvchiga xabar yuboriladi.")
