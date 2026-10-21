"""Payment requests → subscriptions against a real PostgreSQL (skipped without TEST_DATABASE_URL)."""

import asyncio
import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database.models import (
    PaymentRequest,
    PaymentRequestStatus,
    Subscription,
    SubscriptionPlan,
    User,
    UserRole,
)
from app.services import notifications, subscriptions
from app.services.errors import Conflict
from app.utils.time import utcnow

pytestmark = pytest.mark.integration
_ids = itertools.count(70_000_001)


@pytest.fixture(autouse=True)
def no_queue(monkeypatch):
    async def noop(_: int) -> None:
        return None

    monkeypatch.setattr(notifications, "enqueue_send", noop)


async def _plan(session, days: int = 30) -> SubscriptionPlan:
    plan = SubscriptionPlan(name="1 oylik", price=Decimal("150000"), duration_days=days)
    session.add(plan)
    await session.flush()
    return plan


async def _request(session, student: User, plan: SubscriptionPlan) -> PaymentRequest:
    return await subscriptions.create_payment_request(
        session, student, plan.id, proof_kind="text", proof_file_id=None, user_comment="karta 1234, 21.10"
    )


async def test_second_pending_request_is_conflict(session, make_user):
    student = await make_user()
    plan = await _plan(session)
    first = await _request(session, student, plan)
    with pytest.raises(Conflict):
        await _request(session, student, plan)
    # The failed insert must not poison the transaction; after cancelling, a new one is fine.
    await subscriptions.cancel_payment_request(session, student, first.id)
    assert (await _request(session, student, plan)).status == PaymentRequestStatus.PENDING


async def test_approval_creates_subscription_and_notification(session, make_user):
    admin = await make_user(role=UserRole.ADMIN)
    student = await make_user()
    plan = await _plan(session)
    request = await _request(session, student, plan)

    sub = await subscriptions.approve_payment_request(session, admin, request.id)

    assert sub.payment_request_id == request.id
    assert sub.ends_at - sub.starts_at == timedelta(days=30)
    assert await subscriptions.has_active_subscription(session, student.id)
    with pytest.raises(Conflict):  # second tap / second admin
        await subscriptions.approve_payment_request(session, admin, request.id)
    with pytest.raises(Conflict):
        await subscriptions.reject_payment_request(session, admin, request.id, reason="x")
    count = await session.scalar(select(func.count()).where(Subscription.user_id == student.id))
    assert count == 1
    inbox_kinds = await session.scalars(
        select(notifications.Notification.kind).where(
            notifications.Notification.dedupe_key == f"sub_activated:{sub.id}"
        )
    )
    assert list(inbox_kinds) == [notifications.KIND_SUB_ACTIVATED]


async def test_renewal_stacks_on_current_period(session, make_user):
    admin = await make_user(role=UserRole.ADMIN)
    student = await make_user()
    plan = await _plan(session)
    first = await subscriptions.approve_payment_request(
        session, admin, (await _request(session, student, plan)).id
    )
    second = await subscriptions.approve_payment_request(
        session, admin, (await _request(session, student, plan)).id
    )
    assert second.starts_at == first.ends_at
    assert await subscriptions.effective_end(session, student.id) == second.ends_at


async def test_concurrent_double_approval_creates_one_subscription(committing_sessionmaker):
    async with committing_sessionmaker() as s:
        admin = User(telegram_id=next(_ids), role=UserRole.ADMIN, first_name="Admin")
        student = User(telegram_id=next(_ids), first_name="Student")
        s.add_all([admin, student])
        await s.flush()
        plan = await _plan(s)
        request = await _request(s, student, plan)
        await s.commit()

    async def approve() -> str:
        async with committing_sessionmaker() as s:
            actor = await s.get(User, admin.id)
            assert actor is not None
            try:
                await subscriptions.approve_payment_request(s, actor, request.id)
                await s.commit()
                return "approved"
            except Conflict:
                await s.rollback()
                return "conflict"

    results = await asyncio.gather(*(approve() for _ in range(5)))

    assert sorted(results) == ["approved"] + ["conflict"] * 4
    async with committing_sessionmaker() as s:
        assert await s.scalar(select(func.count()).where(Subscription.user_id == student.id)) == 1
        status = await s.scalar(select(PaymentRequest.status).where(PaymentRequest.id == request.id))
        assert status == PaymentRequestStatus.APPROVED


async def test_concurrent_grants_for_same_user_stack_without_overlap(committing_sessionmaker):
    """Two periods added at once for one user: the users-row lock serializes the stacking."""
    async with committing_sessionmaker() as s:
        admin = User(telegram_id=next(_ids), role=UserRole.ADMIN, first_name="Admin")
        student = User(telegram_id=next(_ids), first_name="Student")
        s.add_all([admin, student])
        await s.commit()

    async def grant(days: int) -> Subscription:
        async with committing_sessionmaker() as s:
            actor = await s.get(User, admin.id)
            assert actor is not None
            sub = await subscriptions.grant_subscription(s, actor, student.id, days)
            await s.commit()
            return sub

    subs = sorted(await asyncio.gather(grant(30), grant(30), grant(30)), key=lambda x: x.starts_at)
    assert subs[0].starts_at <= utcnow()
    for earlier, later in itertools.pairwise(subs):
        assert later.starts_at == earlier.ends_at  # no overlap, no lost days
