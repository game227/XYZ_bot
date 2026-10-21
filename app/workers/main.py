"""Worker entry point: arq app.workers.main.WorkerSettings"""

import logging
from typing import Any

from arq import cron

from app.bot import create_bot
from app.config import get_settings
from app.database.session import get_engine
from app.services.notifications import RateLimiter
from app.utils.logging import setup_logging
from app.workers import jobs
from app.workers.queue import close_arq_pool, redis_settings

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings)
    ctx["bot"] = create_bot(settings)
    ctx["limiter"] = RateLimiter(settings.broadcast_rate_per_second)
    logger.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["bot"].session.close()
    await close_arq_pool()
    await get_engine().dispose()


class WorkerSettings:
    functions = [jobs.send_notification]
    cron_jobs = [
        cron(jobs.subscription_reminders, minute=set(range(0, 60, 10)), run_at_startup=True, timeout=300),
        cron(jobs.live_reminders, minute=set(range(0, 60, 5)), timeout=120),
        cron(jobs.sweep_notifications, minute=set(range(1, 60, 2)), timeout=60),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = 10
    job_timeout = 600
    max_tries = 3
