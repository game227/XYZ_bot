"""arq jobs. Each job opens its own DB sessions; `ctx["bot"]` is a shared aiogram Bot."""

import logging
import time
from typing import Any

from app.config import get_settings
from app.database.session import get_sessionmaker, session_scope
from app.services import live, monitoring, notifications, quizzes, subscriptions, teacher_trail
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# --- notifications ------------------------------------------------------------------------


async def send_notification(ctx: dict[str, Any], notification_id: int) -> str | None:
    status = await notifications.deliver(ctx["bot"], get_sessionmaker(), notification_id, ctx["limiter"])
    return status.value if status else None


async def sweep_notifications(ctx: dict[str, Any]) -> int:
    """Safety net: re-queue notifications whose enqueue was lost (Redis hiccup, crash)."""
    async with session_scope() as session:
        ids = await notifications.stuck_notification_ids(session, utcnow())
    for notification_id in ids:
        await notifications.enqueue_send(notification_id)
    return len(ids)


async def subscription_reminders(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        return await subscriptions.queue_due_reminders(session)


async def monitor(ctx: dict[str, Any]) -> bool:
    """Worker heartbeat + one alert per bot outage (TZ §18, §19)."""
    await monitoring.beat("worker")
    return await monitoring.check_bot_alive(ctx["bot"], uptime=time.monotonic() - ctx["started_at"])


async def teacher_trail_sweep(ctx: dict[str, Any]) -> int:
    """Delete what ended teacher sessions showed in Telegram (TZ §7.4–7.5)."""
    async with session_scope() as session:
        return await teacher_trail.sweep(ctx["bot"], session)


async def quiz_reminders(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        return await quizzes.queue_unfinished_reminders(session)


async def live_reminders(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        return await live.queue_due_reminders(session, get_settings().live_reminder_minutes)
