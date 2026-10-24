"""Errors and performance monitoring (TZ §18, §19). OWNER: CORE.

- `MetricsMiddleware` counts handled updates, errors and handler time per hour in Redis
  (`xyz:metrics:<YYYYMMDDHH>`, kept 8 days). Admins see them in 📊 Statistika.
- Heartbeats: the bot process refreshes `xyz:hb:bot` every 30 s, the worker refreshes
  `xyz:hb:worker` every minute. The Docker healthchecks (`python -m app.healthcheck`) read
  them, and the worker alerts the superadmins once when the bot heartbeat disappears.
- Optional Sentry (`SENTRY_DSN`): no PII, no local variables (they may hold passwords).

Everything here is best effort: a Redis failure never breaks the request itself.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

METRICS_PREFIX = "xyz:metrics:"
METRICS_TTL_SECONDS = 8 * 24 * 3600
HEARTBEAT_PREFIX = "xyz:hb:"
HEARTBEAT_TTL_SECONDS = 180
BOT_HEARTBEAT_EVERY = 30
SLOW_UPDATE_MS = 2000
BOT_DOWN_ALERT_KEY = "xyz:alert:bot_down"


def _redis():
    from app.services.auth import get_redis

    return get_redis()


def _hour_key(when: datetime) -> str:
    return f"{METRICS_PREFIX}{when.astimezone(UTC):%Y%m%d%H}"


async def _incr(fields: dict[str, int]) -> None:
    try:
        redis = _redis()
        key = _hour_key(datetime.now(UTC))
        for field, amount in fields.items():
            await redis.hincrby(key, field, amount)
        await redis.expire(key, METRICS_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("metrics unavailable: %s", type(exc).__name__)


async def count_error() -> None:
    await _incr({"errors": 1})


class MetricsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            ms = int((time.perf_counter() - started) * 1000)
            await _incr({"updates": 1, "time_ms": ms, "slow": int(ms >= SLOW_UPDATE_MS)})


# --------------------------------------------------------------------------- heartbeats


async def beat(component: str) -> None:
    try:
        await _redis().set(f"{HEARTBEAT_PREFIX}{component}", int(time.time()), ex=HEARTBEAT_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat failed: %s", type(exc).__name__)


async def last_beat(component: str) -> int | None:
    """Seconds since the component's last heartbeat, None if missing."""
    try:
        value = await _redis().get(f"{HEARTBEAT_PREFIX}{component}")
    except Exception:  # noqa: BLE001
        return None
    return None if value is None else max(0, int(time.time()) - int(value))


async def heartbeat_loop(component: str, every: int = BOT_HEARTBEAT_EVERY) -> None:
    while True:
        await beat(component)
        await asyncio.sleep(every)


async def check_bot_alive(bot: Bot) -> bool:
    """Worker: alert superadmins once per outage when the bot heartbeat is gone."""
    alive = await last_beat("bot") is not None
    try:
        redis = _redis()
        if alive:
            if await redis.delete(BOT_DOWN_ALERT_KEY):
                await _alert(bot, "✅ Bot yana ishlayapti.")
        elif await redis.set(BOT_DOWN_ALERT_KEY, 1, nx=True):
            await _alert(bot, "🚨 Bot jarayoni javob bermayapti (heartbeat yo‘q). Serverni tekshiring.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot liveness check failed: %s", type(exc).__name__)
    return alive


async def _alert(bot: Bot, text: str) -> None:
    for admin_id in get_settings().superadmin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 - alerting must never raise
            logger.warning("Failed to deliver monitoring alert to %s", admin_id)


# --------------------------------------------------------------------------- report


@dataclass(slots=True)
class HealthReport:
    bot_beat: int | None = None
    worker_beat: int | None = None
    updates_1h: int = 0
    errors_1h: int = 0
    avg_ms_1h: int = 0
    updates_24h: int = 0
    errors_24h: int = 0
    slow_24h: int = 0
    avg_ms_24h: int = 0


async def health_report(now: datetime | None = None) -> HealthReport:
    now = now or datetime.now(UTC)
    r = HealthReport(bot_beat=await last_beat("bot"), worker_beat=await last_beat("worker"))
    try:
        redis = _redis()
        hours = [await redis.hgetall(_hour_key(now - timedelta(hours=h))) for h in range(24)]
    except Exception:  # noqa: BLE001
        return r

    def total(rows: list[dict[str, Any]], field: str) -> int:
        return sum(int(row.get(field, 0)) for row in rows)

    last, day = hours[:1], hours
    r.updates_1h, r.errors_1h = total(last, "updates"), total(last, "errors")
    r.avg_ms_1h = total(last, "time_ms") // r.updates_1h if r.updates_1h else 0
    r.updates_24h, r.errors_24h, r.slow_24h = total(day, "updates"), total(day, "errors"), total(day, "slow")
    r.avg_ms_24h = total(day, "time_ms") // r.updates_24h if r.updates_24h else 0
    return r


# --------------------------------------------------------------------------- sentry


def init_sentry(settings: Settings, component: str) -> None:
    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else ""
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.environment,
        server_name=component,
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=0.0,
    )
    logger.info("Sentry enabled for %s", component)
