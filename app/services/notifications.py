"""Notifications: automatic + manual broadcasts + student inbox (TZ §10). OWNER: COMMERCE.

Every notification is a `notifications` row; recipients get one `notification_deliveries` row each.
Sending happens in the arq worker (`send_notification` job) with a global rate limit.

`body` is HTML (parse mode of the bot). Escape user-supplied values with `esc()` when composing.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import Select, event, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    AttemptStatus,
    CourseEnrollment,
    DeliveryStatus,
    Lesson,
    LiveSession,
    LiveSessionRegistration,
    LiveSessionStatus,
    Notification,
    NotificationAudience,
    NotificationDelivery,
    NotificationStatus,
    QuizAttempt,
    User,
    UserRole,
    UserSettings,
    UserStatus,
)
from app.services.audit import log_action
from app.services.errors import Conflict, InvalidInput, NotFound
from app.utils.text import esc
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# Kinds (free strings stored in notifications.kind).
KIND_MANUAL = "manual"
KIND_SUB_ACTIVATED = "sub_activated"
KIND_SUB_EXPIRING_3D = "sub_expiring_3d"
KIND_SUB_EXPIRING_1D = "sub_expiring_1d"
KIND_SUB_EXPIRED = "sub_expired"
KIND_PAYMENT_REJECTED = "payment_rejected"
KIND_NEW_LESSON = "new_lesson"
KIND_LIVE_REMINDER = "live_reminder"
KIND_MEDIA_READY = "media_ready"
KIND_LIVE_CHANGED = "live_changed"
KIND_NEW_QUIZ = "new_quiz"
KIND_QUIZ_UNFINISHED = "quiz_unfinished"

# Sent even to users who turned notifications off (account/payment matters).
CRITICAL_KINDS = {
    KIND_SUB_ACTIVATED,
    KIND_SUB_EXPIRING_3D,
    KIND_SUB_EXPIRING_1D,
    KIND_SUB_EXPIRED,
    KIND_PAYMENT_REJECTED,
    KIND_MEDIA_READY,
}

KIND_LABELS = {
    KIND_MANUAL: "📢 E’lon",
    KIND_SUB_ACTIVATED: "✅ Obuna",
    KIND_SUB_EXPIRING_3D: "⏳ Obuna",
    KIND_SUB_EXPIRING_1D: "⏳ Obuna",
    KIND_SUB_EXPIRED: "⌛️ Obuna",
    KIND_PAYMENT_REJECTED: "🧾 To‘lov",
    KIND_NEW_LESSON: "🆕 Yangi dars",
    KIND_LIVE_REMINDER: "📅 Jonli dars",
    KIND_LIVE_CHANGED: "📅 Jonli dars",
    KIND_MEDIA_READY: "🎬 Media",
    KIND_NEW_QUIZ: "🧪 Yangi test",
    KIND_QUIZ_UNFINISHED: "🧪 Test",
}

SEND_JOB = "send_notification"
BATCH_SIZE = 100
MAX_SEND_TRIES = 3

_background: set[asyncio.Task[None]] = set()


# --- creating -----------------------------------------------------------------------------


def enqueue_after_commit(session: AsyncSession, notification_id: int) -> None:
    """Queue the send job only once the row is committed (a sweeper cron is the safety net)."""

    def _on_commit(_: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(enqueue_send(notification_id))
        _background.add(task)
        task.add_done_callback(_background.discard)

    event.listen(session.sync_session, "after_commit", _on_commit, once=True)


async def enqueue_send(notification_id: int) -> None:
    try:
        from app.workers.queue import get_arq_pool

        pool = await get_arq_pool()
        await pool.enqueue_job(SEND_JOB, notification_id, _job_id=f"notification:{notification_id}")
    except Exception:  # noqa: BLE001 - Redis down: the sweeper re-queues QUEUED notifications
        logger.warning("could not enqueue notification %s", notification_id, exc_info=True)


async def enqueue_broadcast(notification_id: int) -> None:
    """Contract API: queue sending of an already QUEUED notification."""
    await enqueue_send(notification_id)


async def notify_user(
    session: AsyncSession,
    user_id: int,
    kind: str,
    body: str,
    *,
    dedupe_key: str | None = None,
    title: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Notification | None:
    """Notify one user (users.id). Returns None if `dedupe_key` was already used (idempotent).

    Sent by the worker after the caller's transaction commits; shown in the student's 🔔 inbox.
    """
    values = {
        "kind": kind,
        "title": title,
        "body": body,
        "audience_type": NotificationAudience.SINGLE,
        "audience": {"user_ids": [user_id], **(meta or {})},
        "dedupe_key": dedupe_key,
        "status": NotificationStatus.QUEUED,
        "total_count": 1,
    }
    stmt = insert(Notification).values(**values).returning(Notification.id)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
    notification_id = await session.scalar(stmt)
    if notification_id is None:
        return None
    await session.execute(
        insert(NotificationDelivery)
        .values(notification_id=notification_id, user_id=user_id)
        .on_conflict_do_nothing()
    )
    enqueue_after_commit(session, notification_id)
    return await session.get(Notification, notification_id)


async def notify_new_lesson(session: AsyncSession, lesson_id: int) -> Notification | None:
    """Tell students enrolled in the lesson's course about a newly published lesson (once per lesson)."""
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None or not lesson.is_active:
        return None
    stmt = (
        insert(Notification)
        .values(
            kind=KIND_NEW_LESSON,
            title="Yangi dars",
            body=(
                f"🆕 Yangi dars qo‘shildi: <b>{esc(lesson.title)}</b>\n\n"
                "📚 Fanlar va kurslar bo‘limida oching."
            ),
            audience_type=NotificationAudience.COURSE,
            audience={"course_id": lesson.course_id, "lesson_id": lesson.id},
            dedupe_key=f"new_lesson:{lesson.id}",
            status=NotificationStatus.QUEUED,
        )
        .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
        .returning(Notification.id)
    )
    notification_id = await session.scalar(stmt)
    if notification_id is None:
        return None
    enqueue_after_commit(session, notification_id)
    return await session.get(Notification, notification_id)


async def notify_new_quiz(
    session: AsyncSession, quiz_id: int, course_id: int, title: str
) -> Notification | None:
    """Tell students enrolled in the course about a newly activated quiz (once per quiz)."""
    stmt = (
        insert(Notification)
        .values(
            kind=KIND_NEW_QUIZ,
            title="Yangi test",
            body=f"🧪 Yangi test qo‘shildi: <b>{esc(title)}</b>\n\n🧪 Testlar bo‘limida ishlab ko‘ring.",
            audience_type=NotificationAudience.COURSE,
            audience={"course_id": course_id, "quiz_id": quiz_id},
            dedupe_key=f"new_quiz:{quiz_id}",
            status=NotificationStatus.QUEUED,
        )
        .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
        .returning(Notification.id)
    )
    notification_id = await session.scalar(stmt)
    if notification_id is None:
        return None
    enqueue_after_commit(session, notification_id)
    return await session.get(Notification, notification_id)


async def notify_live_registrants(session: AsyncSession, live_id: int, body: str) -> Notification | None:
    """One notification to everyone registered for a live session (e.g. it was cancelled)."""
    user_ids = list(
        (
            await session.scalars(
                select(LiveSessionRegistration.user_id).where(
                    LiveSessionRegistration.live_session_id == live_id
                )
            )
        ).all()
    )
    if not user_ids:
        return None
    notification = Notification(
        kind=KIND_LIVE_CHANGED,
        body=body,
        audience_type=NotificationAudience.USERS,
        audience={"user_ids": user_ids, "live_id": live_id},
        status=NotificationStatus.QUEUED,
    )
    session.add(notification)
    await session.flush()
    enqueue_after_commit(session, notification.id)
    return notification


AUDIENCE_TYPES_MANUAL = (
    NotificationAudience.ALL,
    NotificationAudience.ROLE,
    NotificationAudience.COURSE,
    NotificationAudience.USERS,
)


async def create_broadcast(
    session: AsyncSession,
    actor: User,
    *,
    body: str,
    audience_type: NotificationAudience,
    audience: dict[str, Any],
    title: str | None = None,
) -> Notification:
    """Draft of a manual broadcast. `body` is plain admin text (escaped here)."""
    body = body.strip()
    if not body or len(body) > 3500:
        raise InvalidInput("Xabar matni 1–3500 belgidan iborat bo‘lishi kerak.")
    if audience_type not in AUDIENCE_TYPES_MANUAL:
        raise InvalidInput()
    notification = Notification(
        kind=KIND_MANUAL,
        title=title,
        body=esc(body),
        audience_type=audience_type,
        audience=audience,
        status=NotificationStatus.DRAFT,
        created_by_user_id=actor.id,
    )
    session.add(notification)
    await session.flush()
    return notification


async def confirm_broadcast(session: AsyncSession, actor: User, notification_id: int) -> Notification:
    result = await session.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.status == NotificationStatus.DRAFT)
        .values(status=NotificationStatus.QUEUED)
        .returning(Notification.id)
    )
    if result.scalar_one_or_none() is None:
        raise Conflict("Bu xabar allaqachon yuborilgan yoki bekor qilingan.")
    notification = await session.get(Notification, notification_id)
    assert notification is not None
    await session.refresh(notification)
    await session.execute(
        update(Notification)
        .where(Notification.id == notification_id)
        .values(total_count=await count_recipients(session, notification))
    )
    await log_action(
        session,
        "notification.sent",
        actor=actor,
        entity_type="notification",
        entity_id=notification_id,
        details={"audience_type": notification.audience_type.value, "audience": notification.audience},
    )
    enqueue_after_commit(session, notification_id)
    return notification


async def cancel_broadcast(session: AsyncSession, actor: User, notification_id: int) -> None:
    result = await session.execute(
        update(Notification)
        .where(
            Notification.id == notification_id,
            Notification.status.in_(
                [NotificationStatus.DRAFT, NotificationStatus.QUEUED, NotificationStatus.SENDING]
            ),
        )
        .values(status=NotificationStatus.CANCELLED, finished_at=utcnow())
        .returning(Notification.id)
    )
    if result.scalar_one_or_none() is None:
        raise Conflict("Xabarni bekor qilib bo‘lmaydi.")
    await log_action(
        session, "notification.cancelled", actor=actor, entity_type="notification", entity_id=notification_id
    )


# --- audience -----------------------------------------------------------------------------


def recipients_query(notification: Notification) -> Select[tuple[int]]:
    """users.id of everyone who should get this notification."""
    stmt = select(User.id).where(User.status == UserStatus.ACTIVE)
    audience = notification.audience or {}
    match notification.audience_type:
        case NotificationAudience.ALL:
            pass
        case NotificationAudience.ROLE:
            stmt = stmt.where(User.role == UserRole(audience["role"]))
        case NotificationAudience.COURSE:
            stmt = stmt.join(CourseEnrollment, CourseEnrollment.user_id == User.id).where(
                CourseEnrollment.course_id == int(audience["course_id"])
            )
        case NotificationAudience.USERS | NotificationAudience.SINGLE:
            stmt = stmt.where(User.id.in_([int(i) for i in audience.get("user_ids", [])]))
    if notification.audience_type != NotificationAudience.SINGLE:
        stmt = stmt.where(User.bot_blocked_at.is_(None))
    if notification.kind not in CRITICAL_KINDS:
        stmt = stmt.outerjoin(UserSettings, UserSettings.user_id == User.id).where(
            func.coalesce(UserSettings.notifications_enabled, True).is_(True)
        )
    return stmt


async def count_recipients(session: AsyncSession, notification: Notification) -> int:
    return (
        await session.scalar(select(func.count()).select_from(recipients_query(notification).subquery())) or 0
    )


async def materialize_deliveries(session: AsyncSession, notification: Notification) -> int:
    if notification.audience_type != NotificationAudience.SINGLE:
        sub = recipients_query(notification).subquery()
        await session.execute(
            insert(NotificationDelivery)
            .from_select(["notification_id", "user_id"], select(literal(notification.id), sub.c.id))
            .on_conflict_do_nothing()
        )
    return (
        await session.scalar(
            select(func.count()).where(NotificationDelivery.notification_id == notification.id)
        )
        or 0
    )


# --- staleness re-checks (run right before sending) --------------------------------------

Recheck = Callable[[AsyncSession, Notification, datetime], Awaitable[bool]]


async def _recheck_expiring(session: AsyncSession, n: Notification, now: datetime) -> bool:
    from app.services.subscriptions import effective_end

    user_id = int(n.audience["user_ids"][0])
    expected = datetime.fromisoformat(n.audience["ends_at"])
    current = await effective_end(session, user_id)
    # Renewed (later end), cancelled (no end) or already expired: the reminder is stale.
    return current is not None and current == expected and current > now


async def _recheck_expired(session: AsyncSession, n: Notification, now: datetime) -> bool:
    from app.services.subscriptions import has_active_subscription

    return not await has_active_subscription(session, int(n.audience["user_ids"][0]), now=now)


async def _recheck_live(session: AsyncSession, n: Notification, now: datetime) -> bool:
    live = await session.get(LiveSession, int(n.audience["live_id"]))
    return (
        live is not None
        and live.status == LiveSessionStatus.SCHEDULED
        and live.starts_at.isoformat() == n.audience["starts_at"]
        and live.starts_at > now
    )


async def _recheck_quiz_unfinished(session: AsyncSession, n: Notification, now: datetime) -> bool:
    attempt = await session.get(QuizAttempt, int(n.audience["attempt_id"]))
    return (
        attempt is not None
        and attempt.status == AttemptStatus.IN_PROGRESS
        and (attempt.deadline_at is None or attempt.deadline_at > now)
    )


RECHECKS: dict[str, Recheck] = {
    KIND_SUB_EXPIRING_3D: _recheck_expiring,
    KIND_SUB_EXPIRING_1D: _recheck_expiring,
    KIND_SUB_EXPIRED: _recheck_expired,
    KIND_LIVE_REMINDER: _recheck_live,
    KIND_QUIZ_UNFINISHED: _recheck_quiz_unfinished,
}


# --- sending (worker) ---------------------------------------------------------------------


class RateLimiter:
    """At most `rate` sends per second (spaced evenly), shared by one worker process."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / max(rate, 0.1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


@dataclass(frozen=True, slots=True)
class SendResult:
    status: DeliveryStatus
    message_id: int | None = None
    error: str | None = None


def render(notification: Notification) -> str:
    if notification.title:
        return f"<b>{esc(notification.title)}</b>\n\n{notification.body}"
    return notification.body


async def send_one(bot: Bot, chat_id: int, text: str, limiter: RateLimiter) -> SendResult:
    for attempt in range(MAX_SEND_TRIES):
        await limiter.wait()
        try:
            message = await bot.send_message(chat_id, text)
            return SendResult(DeliveryStatus.SENT, message.message_id)
        except TelegramRetryAfter as exc:
            logger.info("RetryAfter %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            return SendResult(DeliveryStatus.BLOCKED, error="bot blocked by user")
        except TelegramBadRequest as exc:
            return SendResult(DeliveryStatus.FAILED, error=str(exc.message)[:500])
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.info("temporary send error (try %s): %s", attempt + 1, exc)
            await asyncio.sleep(2**attempt)
    return SendResult(DeliveryStatus.FAILED, error="retries exhausted")


async def deliver(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], notification_id: int, limiter: RateLimiter
) -> NotificationStatus | None:
    """Send all pending deliveries of a notification. Idempotent: only `pending` rows are sent."""
    async with sessionmaker() as session:
        notification = await session.get(Notification, notification_id, with_for_update=True)
        if notification is None or notification.status not in (
            NotificationStatus.QUEUED,
            NotificationStatus.SENDING,
        ):
            return notification.status if notification else None
        if notification.status == NotificationStatus.QUEUED:
            notification.total_count = await materialize_deliveries(session, notification)
            notification.status = NotificationStatus.SENDING
            notification.started_at = utcnow()
        await session.commit()

    final = NotificationStatus.DONE
    while True:
        async with sessionmaker() as session:
            notification = await session.get(Notification, notification_id)
            if notification is None or notification.status == NotificationStatus.CANCELLED:
                return NotificationStatus.CANCELLED
            recheck = RECHECKS.get(notification.kind)
            if recheck is not None and not await recheck(session, notification, utcnow()):
                await session.execute(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.notification_id == notification_id,
                        NotificationDelivery.status == DeliveryStatus.PENDING,
                    )
                    .values(status=DeliveryStatus.FAILED, error="stale: no longer relevant")
                )
                await session.commit()
                final = NotificationStatus.CANCELLED
                break
            text = render(notification)
            batch = (
                await session.execute(
                    select(NotificationDelivery.id, User.telegram_id)
                    .join(User, User.id == NotificationDelivery.user_id)
                    .where(
                        NotificationDelivery.notification_id == notification_id,
                        NotificationDelivery.status == DeliveryStatus.PENDING,
                    )
                    .order_by(NotificationDelivery.id)
                    .limit(BATCH_SIZE)
                )
            ).all()
        if not batch:
            break

        results = [
            (delivery_id, telegram_id, await send_one(bot, telegram_id, text, limiter))
            for delivery_id, telegram_id in batch
        ]

        async with sessionmaker() as session:
            now = utcnow()
            for delivery_id, telegram_id, result in results:
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(
                        status=result.status,
                        attempts=NotificationDelivery.attempts + 1,
                        error=result.error,
                        telegram_message_id=result.message_id,
                        sent_at=now if result.status == DeliveryStatus.SENT else None,
                    )
                )
                if result.status == DeliveryStatus.BLOCKED:
                    await session.execute(
                        update(User).where(User.telegram_id == telegram_id).values(bot_blocked_at=now)
                    )
            await session.commit()

    async with sessionmaker() as session:
        counts = dict(
            (
                await session.execute(
                    select(NotificationDelivery.status, func.count())
                    .where(NotificationDelivery.notification_id == notification_id)
                    .group_by(NotificationDelivery.status)
                )
            ).all()
        )
        await session.execute(
            update(Notification)
            .where(Notification.id == notification_id, Notification.status == NotificationStatus.SENDING)
            .values(
                status=final,
                finished_at=utcnow(),
                sent_count=counts.get(DeliveryStatus.SENT, 0),
                failed_count=counts.get(DeliveryStatus.FAILED, 0) + counts.get(DeliveryStatus.BLOCKED, 0),
            )
        )
        await session.commit()
    return final


async def stuck_notification_ids(session: AsyncSession, now: datetime) -> list[int]:
    """QUEUED for >1 min or SENDING for >15 min without progress: re-queue (sweeper)."""
    stmt = select(Notification.id).where(
        (
            (Notification.status == NotificationStatus.QUEUED)
            & (Notification.created_at < now - timedelta(minutes=1))
        )
        | (
            (Notification.status == NotificationStatus.SENDING)
            & (Notification.started_at < now - timedelta(minutes=15))
        )
    )
    return list((await session.scalars(stmt.limit(100))).all())


# --- student inbox ------------------------------------------------------------------------


async def inbox(
    session: AsyncSession, user_id: int, *, limit: int = 10, offset: int = 0
) -> tuple[list[Notification], int]:
    base = (
        select(Notification)
        .join(NotificationDelivery, NotificationDelivery.notification_id == Notification.id)
        .where(NotificationDelivery.user_id == user_id, NotificationDelivery.status == DeliveryStatus.SENT)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = await session.scalars(
        base.order_by(NotificationDelivery.sent_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.all()), total


async def get_inbox_item(session: AsyncSession, user_id: int, notification_id: int) -> Notification:
    """A notification the user actually received (never someone else's)."""
    notification = await session.scalar(
        select(Notification)
        .join(NotificationDelivery, NotificationDelivery.notification_id == Notification.id)
        .where(
            Notification.id == notification_id,
            NotificationDelivery.user_id == user_id,
            NotificationDelivery.status == DeliveryStatus.SENT,
        )
    )
    if notification is None:
        raise NotFound()
    return notification


async def notifications_enabled(session: AsyncSession, user_id: int) -> bool:
    value = await session.scalar(
        select(UserSettings.notifications_enabled).where(UserSettings.user_id == user_id)
    )
    return True if value is None else bool(value)


async def set_notifications_enabled(session: AsyncSession, user_id: int, enabled: bool) -> None:
    stmt = insert(UserSettings).values(user_id=user_id, notifications_enabled=enabled)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[UserSettings.user_id], set_={"notifications_enabled": enabled}
        )
    )


async def recent_broadcasts(session: AsyncSession, *, limit: int = 10, offset: int = 0) -> list[Notification]:
    rows = await session.scalars(
        select(Notification)
        .where(Notification.kind == KIND_MANUAL)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.all())
