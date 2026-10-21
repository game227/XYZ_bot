"""Plans, payment requests and subscriptions (TZ §6). OWNER: COMMERCE.

`get_active_subscription` / `has_active_subscription` are used by every module; keep their
signatures stable.

Rules (CONTRACTS §7 "Payments"):
- At most one pending request per user (partial unique index).
- Approval is a conditional `UPDATE ... WHERE status='pending' RETURNING`; the subscription it
  creates carries a UNIQUE payment_request_id, so a request can never create two subscriptions.
- Renewals stack: a new period starts at max(now, current end).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PaymentRequest,
    PaymentRequestStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    User,
)
from app.services import notifications
from app.services.audit import log_action
from app.services.errors import Conflict, InvalidInput, NotFound
from app.utils.text import esc
from app.utils.time import fmt_dt, utcnow

PROOF_KINDS = ("photo", "document", "text")
MAX_DURATION_DAYS = 3660
EXPIRED_NOTICE_WINDOW = timedelta(days=2)


async def get_active_subscription(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> Subscription | None:
    """The subscription covering `now` (latest ending first), or None. `user_id` is users.id."""
    now = now or utcnow()
    stmt = (
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.starts_at <= now,
            Subscription.ends_at > now,
        )
        .order_by(Subscription.ends_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def has_active_subscription(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> bool:
    return await get_active_subscription(session, user_id, now=now) is not None


async def effective_end(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> datetime | None:
    """End of the user's paid coverage including stacked renewals, or None if nothing runs past `now`."""
    now = now or utcnow()
    return await session.scalar(
        select(func.max(Subscription.ends_at)).where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.ends_at > now,
        )
    )


def compute_period(now: datetime, current_end: datetime | None, days: int) -> tuple[datetime, datetime]:
    """Stacking rule: the new period starts when the current one ends (or now)."""
    starts = max(now, current_end) if current_end else now
    return starts, starts + timedelta(days=days)


def format_remaining(delta: timedelta) -> str:
    if delta.total_seconds() <= 0:
        return "tugagan"
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days} kun {hours} soat" if hours else f"{days} kun"
    if hours:
        return f"{hours} soat {rem // 60} daqiqa"
    return f"{max(rem // 60, 1)} daqiqa"


def format_price(price: Decimal, currency: str = "UZS") -> str:
    whole = f"{price:,.2f}".replace(",", " ").removesuffix(".00")
    return f"{whole} {'so‘m' if currency == 'UZS' else currency}"


# --- plans --------------------------------------------------------------------------------


def parse_price(text: str) -> Decimal:
    try:
        value = Decimal(text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise InvalidInput("Narxni raqamda kiriting, masalan: 150000") from None
    if not value.is_finite() or value <= 0 or value >= Decimal("1e10"):
        raise InvalidInput("Narx musbat son bo‘lishi kerak.")
    return value.quantize(Decimal("0.01"))


def parse_days(text: str) -> int:
    try:
        days = int(text.strip())
    except ValueError:
        raise InvalidInput("Kunlar sonini butun son bilan kiriting, masalan: 30") from None
    if not 1 <= days <= MAX_DURATION_DAYS:
        raise InvalidInput(f"Muddat 1 dan {MAX_DURATION_DAYS} kungacha bo‘lishi kerak.")
    return days


def clean_text(value: str | None, limit: int, *, required: bool = False) -> str | None:
    value = (value or "").strip()
    if required and not value:
        raise InvalidInput("Qiymat bo‘sh bo‘lmasligi kerak.")
    if len(value) > limit:
        raise InvalidInput(f"Matn {limit} belgidan oshmasligi kerak.")
    return value or None


async def list_plans(session: AsyncSession, *, active_only: bool = True) -> list[SubscriptionPlan]:
    stmt = select(SubscriptionPlan).order_by(SubscriptionPlan.position, SubscriptionPlan.id)
    if active_only:
        stmt = stmt.where(SubscriptionPlan.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


async def get_plan(session: AsyncSession, plan_id: int) -> SubscriptionPlan:
    plan = await session.get(SubscriptionPlan, plan_id)
    if plan is None:
        raise NotFound("Tarif topilmadi.")
    return plan


async def create_plan(
    session: AsyncSession,
    actor: User,
    *,
    name: str,
    price: Decimal,
    duration_days: int,
    description: str | None = None,
    terms: str | None = None,
) -> SubscriptionPlan:
    plan = SubscriptionPlan(
        name=clean_text(name, 255, required=True),
        price=price,
        duration_days=duration_days,
        description=clean_text(description, 2000),
        terms=clean_text(terms, 2000),
        position=(await session.scalar(select(func.coalesce(func.max(SubscriptionPlan.position), 0)))) + 1,
    )
    session.add(plan)
    await session.flush()
    await log_action(
        session,
        "plan.created",
        actor=actor,
        entity_type="plan",
        entity_id=plan.id,
        details={"name": plan.name, "price": str(price), "duration_days": duration_days},
    )
    return plan


PLAN_FIELDS = ("name", "price", "duration_days", "description", "terms")


async def update_plan(
    session: AsyncSession, actor: User, plan_id: int, field: str, raw: str
) -> SubscriptionPlan:
    """Change one plan field from admin text input. Existing requests keep their snapshot."""
    plan = await get_plan(session, plan_id)
    old = getattr(plan, field, None)
    match field:
        case "name":
            plan.name = clean_text(raw, 255, required=True)  # type: ignore[assignment]
        case "price":
            plan.price = parse_price(raw)
        case "duration_days":
            plan.duration_days = parse_days(raw)
        case "description" | "terms":
            setattr(plan, field, None if raw.strip() == "-" else clean_text(raw, 2000))
        case _:
            raise InvalidInput()
    await log_action(
        session,
        "plan.updated",
        actor=actor,
        entity_type="plan",
        entity_id=plan.id,
        details={"field": field, "old": str(old), "new": str(getattr(plan, field))},
    )
    return plan


async def toggle_plan(session: AsyncSession, actor: User, plan_id: int) -> SubscriptionPlan:
    plan = await get_plan(session, plan_id)
    plan.is_active = not plan.is_active
    await log_action(
        session,
        "plan.updated",
        actor=actor,
        entity_type="plan",
        entity_id=plan.id,
        details={"field": "is_active", "new": plan.is_active},
    )
    return plan


# --- payment requests ---------------------------------------------------------------------


async def pending_request_for(session: AsyncSession, user_id: int) -> PaymentRequest | None:
    return await session.scalar(
        select(PaymentRequest).where(
            PaymentRequest.user_id == user_id, PaymentRequest.status == PaymentRequestStatus.PENDING
        )
    )


async def create_payment_request(
    session: AsyncSession,
    user: User,
    plan_id: int,
    *,
    proof_kind: str,
    proof_file_id: str | None,
    user_comment: str | None,
) -> PaymentRequest:
    if proof_kind not in PROOF_KINDS:
        raise InvalidInput()
    plan = await get_plan(session, plan_id)
    if not plan.is_active:
        raise InvalidInput("Bu tarif hozir mavjud emas.")
    request = PaymentRequest(
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.price,
        currency=plan.currency,
        duration_days=plan.duration_days,
        proof_kind=proof_kind,
        proof_file_id=proof_file_id,
        user_comment=clean_text(user_comment, 1000),
    )
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        raise Conflict("Sizda ko‘rib chiqilayotgan so‘rov bor. Administrator javobini kuting.") from None
    return request


async def cancel_payment_request(session: AsyncSession, user: User, request_id: int) -> None:
    result = await session.execute(
        update(PaymentRequest)
        .where(
            PaymentRequest.id == request_id,
            PaymentRequest.user_id == user.id,
            PaymentRequest.status == PaymentRequestStatus.PENDING,
        )
        .values(status=PaymentRequestStatus.CANCELLED, decided_at=utcnow())
        .returning(PaymentRequest.id)
    )
    if result.scalar_one_or_none() is None:
        raise Conflict("So‘rov allaqachon ko‘rib chiqilgan.")


async def get_request(session: AsyncSession, request_id: int) -> PaymentRequest:
    request = await session.get(PaymentRequest, request_id)
    if request is None:
        raise NotFound("So‘rov topilmadi.")
    return request


async def list_requests(
    session: AsyncSession,
    status: PaymentRequestStatus = PaymentRequestStatus.PENDING,
    *,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[PaymentRequest], int]:
    base = select(PaymentRequest).where(PaymentRequest.status == status)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    order = (
        PaymentRequest.created_at.asc()
        if status == PaymentRequestStatus.PENDING
        else PaymentRequest.decided_at.desc()
    )
    rows = await session.scalars(base.order_by(order, PaymentRequest.id).limit(limit).offset(offset))
    return list(rows.all()), total


async def _create_subscription(
    session: AsyncSession,
    *,
    user_id: int,
    days: int,
    plan_id: int | None,
    payment_request_id: int | None,
    granted_by: User,
) -> Subscription:
    # Serialize period computation per user (concurrent approvals/grants for the same user).
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    now = utcnow()
    starts, ends = compute_period(now, await effective_end(session, user_id, now=now), days)
    subscription = Subscription(
        user_id=user_id,
        plan_id=plan_id,
        payment_request_id=payment_request_id,
        status=SubscriptionStatus.ACTIVE,
        starts_at=starts,
        ends_at=ends,
        granted_by_user_id=granted_by.id,
    )
    session.add(subscription)
    await session.flush()
    plan_name = (
        await session.scalar(select(SubscriptionPlan.name).where(SubscriptionPlan.id == plan_id))
        if plan_id
        else None
    )
    await notifications.notify_user(
        session,
        user_id,
        notifications.KIND_SUB_ACTIVATED,
        "✅ Obunangiz faollashtirildi!\n\n"
        + (f"Tarif: <b>{esc(plan_name)}</b>\n" if plan_name else "")
        + f"Boshlanish: {fmt_dt(starts)}\nTugash: {fmt_dt(ends)}",
        dedupe_key=f"sub_activated:{subscription.id}",
    )
    return subscription


async def approve_payment_request(
    session: AsyncSession, admin: User, request_id: int, *, comment: str | None = None
) -> Subscription:
    """Idempotent: a second approval (double tap, two admins) raises Conflict and creates nothing."""
    now = utcnow()
    request = (
        await session.execute(
            update(PaymentRequest)
            .where(PaymentRequest.id == request_id, PaymentRequest.status == PaymentRequestStatus.PENDING)
            .values(
                status=PaymentRequestStatus.APPROVED,
                reviewed_by_user_id=admin.id,
                decided_at=now,
                decision_comment=clean_text(comment, 1000),
            )
            .returning(PaymentRequest)
        )
    ).scalar_one_or_none()
    if request is None:
        raise Conflict("Bu so‘rov allaqachon ko‘rib chiqilgan.")
    subscription = await _create_subscription(
        session,
        user_id=request.user_id,
        days=request.duration_days,
        plan_id=request.plan_id,
        payment_request_id=request.id,
        granted_by=admin,
    )
    await log_action(
        session,
        "payment_request.approved",
        actor=admin,
        entity_type="payment_request",
        entity_id=request.id,
        details={
            "user_id": request.user_id,
            "amount": str(request.amount),
            "subscription_id": subscription.id,
        },
    )
    await log_action(
        session,
        "subscription.activated",
        actor=admin,
        entity_type="subscription",
        entity_id=subscription.id,
        details={
            "user_id": request.user_id,
            "starts_at": subscription.starts_at.isoformat(),
            "ends_at": subscription.ends_at.isoformat(),
        },
    )
    return subscription


async def reject_payment_request(
    session: AsyncSession, admin: User, request_id: int, *, reason: str | None = None
) -> PaymentRequest:
    reason = clean_text(reason, 1000)
    request = (
        await session.execute(
            update(PaymentRequest)
            .where(PaymentRequest.id == request_id, PaymentRequest.status == PaymentRequestStatus.PENDING)
            .values(
                status=PaymentRequestStatus.REJECTED,
                reviewed_by_user_id=admin.id,
                decided_at=utcnow(),
                decision_comment=reason,
            )
            .returning(PaymentRequest)
        )
    ).scalar_one_or_none()
    if request is None:
        raise Conflict("Bu so‘rov allaqachon ko‘rib chiqilgan.")
    await notifications.notify_user(
        session,
        request.user_id,
        notifications.KIND_PAYMENT_REJECTED,
        f"❌ #{request.id} obuna so‘rovingiz rad etildi."
        + (f"\nSabab: {esc(reason)}" if reason else "")
        + "\n\nSavollar bo‘lsa, administratorga murojaat qiling.",
        dedupe_key=f"payment_rejected:{request.id}",
    )
    await log_action(
        session,
        "payment_request.rejected",
        actor=admin,
        entity_type="payment_request",
        entity_id=request.id,
        details={"user_id": request.user_id, "reason": reason},
    )
    return request


async def grant_subscription(
    session: AsyncSession, admin: User, user_id: int, days: int, *, plan_id: int | None = None
) -> Subscription:
    """Manual grant/extension by an admin (no payment request)."""
    subscription = await _create_subscription(
        session, user_id=user_id, days=days, plan_id=plan_id, payment_request_id=None, granted_by=admin
    )
    await log_action(
        session,
        "subscription.granted",
        actor=admin,
        entity_type="subscription",
        entity_id=subscription.id,
        details={"user_id": user_id, "days": days, "ends_at": subscription.ends_at.isoformat()},
    )
    return subscription


async def cancel_subscriptions(session: AsyncSession, admin: User, user_id: int) -> int:
    """Cancel every current/future subscription of the user (paid access closes immediately)."""
    now = utcnow()
    ids = (
        await session.scalars(
            update(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.ends_at > now,
            )
            .values(status=SubscriptionStatus.CANCELLED, cancelled_at=now)
            .returning(Subscription.id)
        )
    ).all()
    if ids:
        await log_action(
            session,
            "subscription.cancelled",
            actor=admin,
            entity_type="user",
            entity_id=user_id,
            details={"subscription_ids": list(ids)},
        )
    return len(ids)


# --- status -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    active: bool
    plan_name: str | None
    starts_at: datetime | None
    ends_at: datetime | None  # end of coverage incl. stacked renewals
    remaining: timedelta | None
    last_ended_at: datetime | None


async def subscription_view(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> SubscriptionView:
    now = now or utcnow()
    current = await get_active_subscription(session, user_id, now=now)
    if current is None:
        last_end = await session.scalar(
            select(func.max(Subscription.ends_at)).where(
                Subscription.user_id == user_id, Subscription.ends_at <= now
            )
        )
        return SubscriptionView(False, None, None, None, None, last_end)
    end = await effective_end(session, user_id, now=now) or current.ends_at
    plan_name = (
        await session.scalar(select(SubscriptionPlan.name).where(SubscriptionPlan.id == current.plan_id))
        if current.plan_id
        else None
    )
    return SubscriptionView(True, plan_name, current.starts_at, end, end - now, None)


# --- reminders (worker cron) --------------------------------------------------------------


def classify_reminder(end: datetime, now: datetime) -> str | None:
    """Which notice is due for coverage ending at `end`: 3d / 1d before, or expired."""
    remaining = end - now
    if remaining <= timedelta(0):
        return notifications.KIND_SUB_EXPIRED if -remaining < EXPIRED_NOTICE_WINDOW else None
    if remaining <= timedelta(days=1):
        return notifications.KIND_SUB_EXPIRING_1D
    if remaining <= timedelta(days=3):
        return notifications.KIND_SUB_EXPIRING_3D
    return None


async def reminder_candidates(session: AsyncSession, now: datetime) -> list[tuple[int, datetime]]:
    """(user_id, coverage end) for users whose coverage ends within 3 days or ended recently."""
    end = func.max(Subscription.ends_at)
    stmt = (
        select(Subscription.user_id, end)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
        .group_by(Subscription.user_id)
        .having(end > now - EXPIRED_NOTICE_WINDOW, end <= now + timedelta(days=3))
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def queue_due_reminders(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Create due reminder/expiry notifications. Idempotent via dedupe keys bound to the end date;
    the sender re-checks relevance right before sending (renewed users get nothing)."""
    now = now or utcnow()
    created = 0
    for user_id, end in await reminder_candidates(session, now):
        kind = classify_reminder(end, now)
        if kind is None:
            continue
        if kind == notifications.KIND_SUB_EXPIRED:
            body = (
                f"⌛️ Obunangiz {fmt_dt(end)} da tugadi. Pullik darslarga kirish yopildi, "
                "natijalaringiz saqlanib qoladi.\n\n💳 Obuna olish bo‘limidan yangilashingiz mumkin."
            )
        else:
            body = (
                f"⏳ Obunangiz {fmt_dt(end)} da tugaydi ({format_remaining(end - now)} qoldi).\n\n"
                "Uzluksiz foydalanish uchun 💳 Obuna olish bo‘limidan yangilang."
            )
        notification = await notifications.notify_user(
            session,
            user_id,
            kind,
            body,
            dedupe_key=f"{kind}:{user_id}:{end.isoformat()}",
            meta={"ends_at": end.isoformat()},
        )
        if notification is not None:
            created += 1
            column = {
                notifications.KIND_SUB_EXPIRING_3D: Subscription.reminder_3d_sent_at,
                notifications.KIND_SUB_EXPIRING_1D: Subscription.reminder_1d_sent_at,
                notifications.KIND_SUB_EXPIRED: Subscription.expired_notified_at,
            }[kind]
            await session.execute(
                update(Subscription)
                .where(Subscription.user_id == user_id, Subscription.ends_at == end)
                .values({column.key: now})
            )
            if kind == notifications.KIND_SUB_EXPIRED:
                await log_action(
                    session,
                    "subscription.expired",
                    entity_type="user",
                    entity_id=user_id,
                    details={"ended_at": end.isoformat()},
                )
    return created
