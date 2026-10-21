"""Reminders, dedupe, stale re-checks and delivery bookkeeping against PostgreSQL."""

import itertools
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.database.models import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    NotificationStatus,
    User,
    UserRole,
)
from app.services import notifications, subscriptions
from app.utils.time import utcnow

pytestmark = pytest.mark.integration
_ids = itertools.count(71_000_001)


@pytest.fixture(autouse=True)
def no_queue(monkeypatch):
    async def noop(_: int) -> None:
        return None

    monkeypatch.setattr(notifications, "enqueue_send", noop)


class FakeMessage:
    message_id = 1


class FakeBot:
    def __init__(self, blocked: set[int] | None = None) -> None:
        self.blocked = blocked or set()
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> FakeMessage:
        if chat_id in self.blocked:
            raise TelegramForbiddenError(
                SendMessage(chat_id=chat_id, text=text), "Forbidden: bot was blocked"
            )
        self.sent.append((chat_id, text))
        return FakeMessage()


async def test_notify_user_dedupe(session, make_user):
    user = await make_user()
    first = await notifications.notify_user(session, user.id, "x", "body", dedupe_key="k:1")
    again = await notifications.notify_user(session, user.id, "x", "body", dedupe_key="k:1")
    assert first is not None and again is None


async def test_expiry_reminder_once_and_stale_after_renewal(session, make_user):
    admin = await make_user(role=UserRole.ADMIN)
    student = await make_user()
    await subscriptions.grant_subscription(session, admin, student.id, 2)  # ends in 2 days → 3-day reminder

    assert await subscriptions.queue_due_reminders(session) == 1
    assert await subscriptions.queue_due_reminders(session) == 0  # dedupe
    reminder = await session.scalar(
        select(Notification).where(Notification.kind == notifications.KIND_SUB_EXPIRING_3D)
    )
    assert reminder is not None
    assert await notifications._recheck_expiring(session, reminder, utcnow())

    await subscriptions.grant_subscription(session, admin, student.id, 30)  # renewed before sending
    assert not await notifications._recheck_expiring(session, reminder, utcnow())


async def test_expired_notice_skipped_when_user_has_access_again(session, make_user):
    admin = await make_user(role=UserRole.ADMIN)
    student = await make_user()
    sub = await subscriptions.grant_subscription(session, admin, student.id, 1)
    sub.starts_at -= timedelta(days=2)
    sub.ends_at = utcnow() - timedelta(hours=1)
    await session.flush()
    assert await subscriptions.queue_due_reminders(session) == 1
    expired = await session.scalar(
        select(Notification).where(Notification.kind == notifications.KIND_SUB_EXPIRED)
    )
    assert expired is not None and await notifications._recheck_expired(session, expired, utcnow())
    await subscriptions.grant_subscription(session, admin, student.id, 30)
    assert not await notifications._recheck_expired(session, expired, utcnow())


async def test_broadcast_delivery_marks_blocked_users(committing_sessionmaker):
    async with committing_sessionmaker() as s:
        ok_user = User(telegram_id=next(_ids), first_name="A")
        blocked_user = User(telegram_id=next(_ids), first_name="B")
        admin = User(telegram_id=next(_ids), first_name="C", role=UserRole.ADMIN)
        s.add_all([ok_user, blocked_user, admin])
        await s.flush()
        draft = await notifications.create_broadcast(
            s,
            admin,
            body="Salom <hamma>",
            audience_type=notifications.NotificationAudience.ROLE,
            audience={"role": "student"},
        )
        await notifications.confirm_broadcast(s, admin, draft.id)
        await s.commit()

    bot = FakeBot(blocked={blocked_user.telegram_id})
    status = await notifications.deliver(
        bot, committing_sessionmaker, draft.id, notifications.RateLimiter(1000)
    )  # type: ignore[arg-type]

    assert status == NotificationStatus.DONE
    assert bot.sent == [(ok_user.telegram_id, "Salom &lt;hamma&gt;")]  # admin text is escaped
    async with committing_sessionmaker() as s:
        n = await s.get(Notification, draft.id)
        assert n is not None and (n.total_count, n.sent_count, n.failed_count) == (2, 1, 1)
        statuses = dict(
            (await s.execute(select(NotificationDelivery.user_id, NotificationDelivery.status))).all()
        )
        assert statuses == {ok_user.id: DeliveryStatus.SENT, blocked_user.id: DeliveryStatus.BLOCKED}
        assert (await s.get(User, blocked_user.id)).bot_blocked_at is not None  # type: ignore[union-attr]

    # Re-running the job is a no-op: nothing is sent twice.
    assert (
        await notifications.deliver(bot, committing_sessionmaker, draft.id, notifications.RateLimiter(1000))
        == NotificationStatus.DONE
    )  # type: ignore[arg-type]
    assert len(bot.sent) == 1


async def test_stale_reminder_is_not_sent(committing_sessionmaker):
    async with committing_sessionmaker() as s:
        admin = User(telegram_id=next(_ids), first_name="A", role=UserRole.ADMIN)
        student = User(telegram_id=next(_ids), first_name="S")
        s.add_all([admin, student])
        await s.flush()
        await subscriptions.grant_subscription(s, admin, student.id, 2)
        await subscriptions.queue_due_reminders(s)
        reminder = await s.scalar(
            select(Notification).where(Notification.kind == notifications.KIND_SUB_EXPIRING_3D)
        )
        assert reminder is not None
        await subscriptions.grant_subscription(s, admin, student.id, 30)  # renewal before the worker runs
        await s.commit()

    bot = FakeBot()
    status = await notifications.deliver(
        bot, committing_sessionmaker, reminder.id, notifications.RateLimiter(1000)
    )  # type: ignore[arg-type]
    assert status == NotificationStatus.CANCELLED
    assert all("tugaydi" not in text for _, text in bot.sent)
