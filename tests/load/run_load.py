"""Load test (TZ §18, acceptance criterion 25).

    .venv/bin/python -m tests.load.run_load --steps 50,100,200 --duration 30

Uses ONLY the dedicated database LOAD_DATABASE_URL (default .../xyz_load) and Redis db 14, a dummy
bot token and a fake Telegram session: no request ever reaches Telegram.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

# Must happen before any `app` import: settings are read once and cached.
LOAD_DB = os.environ.get("LOAD_DATABASE_URL", "postgresql+asyncpg://xyz:xyz@localhost:5432/xyz_load")
os.environ["DATABASE_URL"] = LOAD_DB
os.environ["REDIS_URL"] = os.environ.get("LOAD_REDIS_URL", "redis://localhost:6379/14")
os.environ["SUPERADMIN_IDS"] = ""
os.environ["LOG_LEVEL"] = "WARNING"

from tests.load.harness import DUMMY_TOKEN  # noqa: E402

os.environ["BOT_TOKEN"] = DUMMY_TOKEN

from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import app.bot as app_bot  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database.models import LessonAccess, User  # noqa: E402
from app.database.session import get_engine, get_sessionmaker  # noqa: E402
from app.keyboards import menus  # noqa: E402
from app.keyboards.content import CtLes, CtNav, CtQz  # noqa: E402
from app.middlewares.throttling import ThrottlingMiddleware  # noqa: E402
from tests.load import harness  # noqa: E402
from tests.load.harness import METRICS, FakeTelegramSession, callback_update, feed, make_bot, message_update  # noqa: E402
from tests.load.seed import Catalog, LessonInfo, ensure_database, reset_schema, seed  # noqa: E402

MIN_AVAILABLE_MIB = 400
RESULTS_FILE = Path(__file__).with_name("results.json")


class Aborted(Exception):
    pass


def build_dispatcher():
    """The production dispatcher (all routers + middlewares), FSM in memory. Routers are module-level
    singletons, so there is exactly one dispatcher per process."""
    return app_bot.create_dispatcher(get_settings(), storage=MemoryStorage())


def set_throttle(dp, enabled: bool) -> None:
    """Saturation / free-limit runs fire faster than the per-user flood guard allows; lift it there."""
    for mw in dp.update.outer_middleware:
        if isinstance(mw, ThrottlingMiddleware):
            mw.rate = 8 if enabled else 1_000_000
            mw._hits.clear()


class VirtualUser:
    """One student session: /start → catalog → lesson → quiz → profile."""

    def __init__(
        self, dp, bot, fake: FakeTelegramSession, catalog: Catalog, think: tuple[float, float]
    ) -> None:
        self.dp, self.bot, self.fake, self.catalog, self.think = dp, bot, fake, catalog, think

    async def _pause(self) -> None:
        if self.think[1] > 0:
            await asyncio.sleep(random.uniform(*self.think))

    async def msg(self, tg: int, text: str, kind: str) -> None:
        await feed(self.dp, self.bot, message_update(self.bot, tg, text), kind)
        await self._pause()

    async def cb(self, tg: int, data: str, kind: str) -> None:
        await feed(self.dp, self.bot, callback_update(self.bot, tg, data), kind)
        await self._pause()

    def _button(self, tg: int, prefix: str) -> str | None:
        markup = self.fake.last_markup.get(tg)
        if markup is None:
            return None
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data and b.callback_data.startswith(prefix):
                    return b.callback_data
        return None

    def _pick_lesson(self, tg: int) -> LessonInfo:
        if tg in self.catalog.subscriber_tg or random.random() < 0.7:
            pool = self.catalog.lessons if tg in self.catalog.subscriber_tg else self.catalog.free
        else:
            pool = self.catalog.paid  # non-subscriber hits the paywall (expected SubscriptionRequired)
        return random.choice(pool)

    async def run_once(self, tg: int) -> None:
        await self.msg(tg, "/start", "start")
        await self.msg(tg, menus.BTN_COURSES, "catalog")
        les = self._pick_lesson(tg)
        await self.cb(tg, CtNav(level="subject", id=les.subject_id).pack(), "nav")
        await self.cb(tg, CtNav(level="course", id=les.course_id).pack(), "nav")
        await self.cb(tg, CtNav(level="module", id=les.module_id).pack(), "nav")
        await self.cb(tg, CtNav(level="topic", id=les.topic_id).pack(), "nav")
        await self.cb(tg, CtLes(action="open", lesson_id=les.id).pack(), "lesson_open")
        confirm = self._button(tg, "ct_les:confirm")
        if confirm:
            await self.cb(tg, confirm, "lesson_confirm")
        if les.quiz_id and (tg in self.catalog.subscriber_tg or les.is_free):
            await self.cb(tg, CtQz(action="card", quiz_id=les.quiz_id).pack(), "quiz_card")
            if self._button(tg, "ct_qz:start"):
                await self.cb(tg, CtQz(action="start", quiz_id=les.quiz_id).pack(), "quiz_start")
                for _ in range(6):
                    pick = self._button(tg, "ct_qz:pick")
                    if not pick:
                        break
                    await self.cb(tg, pick, "quiz_answer")
        await self.msg(tg, menus.BTN_PROFILE, "profile")


async def sampler(stop: asyncio.Event, state: dict) -> None:
    pool = get_engine().sync_engine.pool
    while not stop.is_set():
        METRICS.rss_samples.append(harness.rss_mib())
        METRICS.pool_samples.append((pool.checkedout(), pool.overflow()))
        avail = harness.mem_available_mib()
        state["min_available_mib"] = min(state.get("min_available_mib", 1e9), avail)
        if avail < MIN_AVAILABLE_MIB:
            state["aborted"] = f"MemAvailable {avail:.0f} MiB < {MIN_AVAILABLE_MIB} MiB"
            stop.set()
            return
        await asyncio.sleep(0.5)


async def run_step(
    label: str,
    dp,
    bot,
    fake,
    catalog: Catalog,
    users: list[int],
    concurrency: int,
    duration: float,
    think: tuple[float, float],
) -> dict:
    METRICS.reset()
    stop = asyncio.Event()
    state: dict = {}
    user_iter = iter(users)

    async def vu_loop() -> None:
        vu = VirtualUser(dp, bot, fake, catalog, think)
        while not stop.is_set():
            try:
                tg = next(user_iter)
            except StopIteration:
                return
            await vu.run_once(tg)

    sampler_task = asyncio.create_task(sampler(stop, state))
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    tasks = [asyncio.create_task(vu_loop()) for _ in range(concurrency)]
    try:
        await asyncio.wait_for(stop.wait(), timeout=duration)
    except TimeoutError:
        pass
    stop.set()
    # Let in-flight scripts finish their current step (bounded).
    done, pending = await asyncio.wait(tasks, timeout=30)
    for t in pending:
        t.cancel()
    elapsed = time.perf_counter() - t0
    await sampler_task
    result = harness.summarize(label, elapsed, concurrency)
    # Share of one core used by this (single-threaded event loop) process; ~100% = CPU-bound.
    result["cpu_util_pct"] = round(100 * (time.process_time() - cpu0) / elapsed, 1)
    result["min_available_mib"] = round(state.get("min_available_mib", 0))
    if state.get("aborted"):
        result["aborted"] = state["aborted"]
    return result


async def free_limit_check(dp, bot, catalog: Catalog, users: list[int], parallel: int = 30) -> dict:
    """Same user fires `parallel` confirm-opens across all free lessons at once."""
    outcomes = []
    for tg in users:
        free = catalog.free
        updates = [
            callback_update(bot, tg, CtLes(action="confirm", lesson_id=free[i % len(free)].id).pack())
            for i in range(parallel)
        ]
        await asyncio.gather(*(feed(dp, bot, u, "free_limit") for u in updates))
        async with get_sessionmaker()() as s:
            consumed = await s.scalar(
                select(func.count(LessonAccess.id))
                .join(User, User.id == LessonAccess.user_id)
                .where(User.telegram_id == tg, LessonAccess.consumed_free_slot.is_(True))
            )
        outcomes.append(consumed)
    limit = get_settings().free_lesson_limit
    return {
        "users": len(users),
        "parallel_opens_each": parallel,
        "consumed_per_user": outcomes,
        "ok": all(c == limit for c in outcomes),
        "limit": limit,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="50,100,200")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--think", default="0.5,1.5", help="min,max seconds between a user's actions")
    ap.add_argument("--saturation", type=int, default=100, help="0 disables the no-think-time run")
    ap.add_argument("--no-seed", action="store_true")
    ap.add_argument("--users", type=int, default=10_000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    errors = harness.ErrorCounter()
    logging.getLogger().addHandler(errors)

    settings = get_settings()
    assert settings.bot_token.get_secret_value() == DUMMY_TOKEN, "refusing to run with a real bot token"
    await ensure_database(settings.database_url)
    engine = get_engine()
    harness.instrument_pool(engine)

    t0 = time.perf_counter()
    if not args.no_seed:
        await reset_schema(engine)
    catalog = await seed(engine, users=args.users) if not args.no_seed else None
    if catalog is None:
        raise SystemExit("--no-seed is not supported yet (catalog map comes from seeding)")
    seed_s = time.perf_counter() - t0
    print(f"seeded {len(catalog.lessons)} lessons, {len(catalog.all_tg)} users in {seed_s:.1f}s", flush=True)

    fake = FakeTelegramSession()
    bot = make_bot(fake)
    think = tuple(float(x) for x in args.think.split(","))
    free_users = [t for t in catalog.all_tg if t not in catalog.subscriber_tg]
    others = [t for t in catalog.all_tg]
    random.Random(1).shuffle(others)
    reserved = set(free_users[:5])
    pool_users = [t for t in others if t not in reserved]

    results = {
        "settings": {
            "db_pool_size": settings.db_pool_size,
            "db_max_overflow": settings.db_max_overflow,
            "telegram_latency_ms": list(fake.latency_ms),
            "think_s": list(think),
            "users_seeded": len(catalog.all_tg),
            "lessons": len(catalog.lessons),
            "seed_seconds": round(seed_s, 1),
        },
        "steps": [],
    }
    dp = build_dispatcher()
    set_throttle(dp, True)
    offset = 0
    for c in [int(x) for x in args.steps.split(",") if x]:
        users = pool_users[offset:]
        res = await run_step(f"steady-{c}", dp, bot, fake, catalog, users, c, args.duration, think)
        offset += res["updates"]  # fresh users per step (upper bound of consumed users)
        res["unhandled_errors_logged"] = errors.count
        results["steps"].append(res)
        print(json.dumps(res, ensure_ascii=False), flush=True)
        if res.get("aborted"):
            break
        await asyncio.sleep(3)

    if args.saturation and not any(s.get("aborted") for s in results["steps"]):
        set_throttle(dp, False)
        res = await run_step(
            f"saturation-{args.saturation}",
            dp,
            bot,
            fake,
            catalog,
            pool_users[offset:],
            args.saturation,
            min(args.duration, 30),
            (0.0, 0.0),
        )
        res["unhandled_errors_logged"] = errors.count
        results["steps"].append(res)
        print(json.dumps(res, ensure_ascii=False), flush=True)

    set_throttle(dp, False)
    METRICS.reset()
    results["free_limit"] = await free_limit_check(dp, bot, catalog, sorted(reserved))
    print(json.dumps(results["free_limit"]), flush=True)

    results["telegram_calls"] = dict(fake.calls)
    results["unhandled_errors"] = {"count": errors.count, "samples": errors.samples}
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
