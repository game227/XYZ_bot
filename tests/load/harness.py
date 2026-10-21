"""Load-test harness: the real aiogram Dispatcher fed with synthetic updates and a fake Bot.

The fake session never touches the network. Every Bot API call is answered locally after a
simulated Telegram latency (30–80 ms by default), and the call is recorded so virtual users
can "press" the inline buttons the bot actually sent them.
"""

import asyncio
import contextvars
import itertools
import logging
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, InlineKeyboardMarkup, Message, Update

# A syntactically valid dummy token: the fake session makes any real call impossible anyway.
DUMMY_TOKEN = "123456789:LOAD-test-token-never-sent-anywhere-0000"

_api_wait: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("api_wait", default=None)


class FakeTelegramSession(BaseSession):
    """BaseSession that answers locally. Records (method, chat_id, reply_markup)."""

    def __init__(self, latency_ms: tuple[int, int] = (30, 80)) -> None:
        super().__init__()
        self.latency_ms = latency_ms
        self.calls: Counter[str] = Counter()
        self.last_markup: dict[int, InlineKeyboardMarkup] = {}
        self.last_text: dict[int, str] = {}
        self.alerts: dict[int, str] = {}
        self._msg_ids = itertools.count(1000)

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover - not used
        raise RuntimeError("downloads are not simulated")
        yield b""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - BaseSession API
    ) -> Any:
        delay = random.uniform(*self.latency_ms) / 1000
        bucket = _api_wait.get()
        if bucket is not None:
            bucket.append(delay)
        await asyncio.sleep(delay)
        name = type(method).__name__
        self.calls[name] += 1

        chat_id = getattr(method, "chat_id", None)
        markup = getattr(method, "reply_markup", None)
        text = getattr(method, "text", None)
        if isinstance(chat_id, int):
            if isinstance(markup, InlineKeyboardMarkup):
                self.last_markup[chat_id] = markup
            if text is not None:
                self.last_text[chat_id] = text
        returning = getattr(method, "__returning__", None)
        if name == "AnswerCallbackQuery":
            return True
        if returning is Message or name in {"EditMessageText", "EditMessageReplyMarkup"}:
            return Message(
                message_id=next(self._msg_ids),
                date=int(time.time()),
                chat=Chat(id=chat_id if isinstance(chat_id, int) else 0, type="private"),
                text=text if isinstance(text, str) else None,
            )
        if returning is bool:
            return True
        if getattr(returning, "__origin__", None) is list:
            return []
        return True


def make_bot(session: FakeTelegramSession) -> Bot:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    return Bot(token=DUMMY_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# --------------------------------------------------------------------------- update factories

_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


def _tg_user(telegram_id: int) -> dict[str, Any]:
    return {
        "id": telegram_id,
        "is_bot": False,
        "first_name": f"Load{telegram_id % 100000}",
        "language_code": "uz",
    }


def message_update(bot: Bot, telegram_id: int, text: str) -> Update:
    data = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "date": int(time.time()),
            "chat": {"id": telegram_id, "type": "private"},
            "from": _tg_user(telegram_id),
            "text": text,
            **(
                {"entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
                if text.startswith("/")
                else {}
            ),
        },
    }
    return Update.model_validate(data, context={"bot": bot})


def callback_update(bot: Bot, telegram_id: int, data: str) -> Update:
    payload = {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_update_ids)),
            "from": _tg_user(telegram_id),
            "chat_instance": str(telegram_id),
            "data": data,
            "message": {
                "message_id": next(_message_ids),
                "date": int(time.time()),
                "chat": {"id": telegram_id, "type": "private"},
                "from": {"id": 123456789, "is_bot": True, "first_name": "XYZ"},
                "text": "…",
            },
        },
    }
    return Update.model_validate(payload, context={"bot": bot})


# --------------------------------------------------------------------------- metrics


@dataclass
class Metrics:
    latencies: list[float] = field(default_factory=list)  # full feed_update time, seconds
    net_latencies: list[float] = field(default_factory=list)  # minus simulated Telegram waits
    by_kind: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: int = 0
    pool_waits: list[float] = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0
    rss_samples: list[int] = field(default_factory=list)
    pool_samples: list[tuple[int, int]] = field(default_factory=list)  # (checked_out, overflow)

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


METRICS = Metrics()


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


async def feed(dp, bot: Bot, update: Update, kind: str) -> None:
    bucket: list[float] = []
    token = _api_wait.set(bucket)
    t0 = time.perf_counter()
    try:
        await dp.feed_update(bot, update)
    except Exception:  # noqa: BLE001 - counted, not raised
        METRICS.errors += 1
        logging.getLogger(__name__).exception("feed_update raised")
    finally:
        dt = time.perf_counter() - t0
        _api_wait.reset(token)
        METRICS.latencies.append(dt)
        METRICS.net_latencies.append(max(0.0, dt - sum(bucket)))
        METRICS.by_kind[kind].append(dt)


class ErrorCounter(logging.Handler):
    """Counts unhandled errors logged by CORE's global error handler."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.samples: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1
        if len(self.samples) < 5:
            exc = record.exc_info[1] if record.exc_info else None
            self.samples.append(
                f"{record.name}: {record.getMessage()} {type(exc).__name__ if exc else ''} {exc or ''}"
            )


def instrument_pool(engine) -> None:
    """Time how long sessions wait for a pooled connection (QueuePool._do_get)."""
    pool = engine.sync_engine.pool
    original = pool._do_get

    def timed_do_get():
        t0 = time.perf_counter()
        try:
            return original()
        finally:
            METRICS.pool_waits.append(time.perf_counter() - t0)

    pool._do_get = timed_do_get


def rss_mib() -> float:
    with open(f"/proc/{os.getpid()}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def mem_available_mib() -> float:
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    return 0.0


def summarize(label: str, duration: float, concurrency: int) -> dict[str, Any]:
    m = METRICS
    lat = [x * 1000 for x in m.latencies]
    net = [x * 1000 for x in m.net_latencies]
    waits = [x * 1000 for x in m.pool_waits]
    return {
        "label": label,
        "concurrency": concurrency,
        "duration_s": round(duration, 1),
        "updates": len(lat),
        "throughput_ups": round(len(lat) / duration, 1) if duration else 0,
        "p50_ms": round(pct(lat, 50), 1),
        "p95_ms": round(pct(lat, 95), 1),
        "p99_ms": round(pct(lat, 99), 1),
        "max_ms": round(max(lat), 1) if lat else 0,
        "net_p50_ms": round(pct(net, 50), 1),
        "net_p95_ms": round(pct(net, 95), 1),
        "net_p99_ms": round(pct(net, 99), 1),
        "pool_checkouts": len(waits),
        "pool_wait_p95_ms": round(pct(waits, 95), 2),
        "pool_wait_max_ms": round(max(waits), 1) if waits else 0,
        "pool_waits_over_10ms": sum(1 for w in waits if w > 10),
        "max_checked_out": max((c for c, _ in m.pool_samples), default=0),
        "errors": m.errors,
        "rss_max_mib": round(max(m.rss_samples, default=0), 1),
        "by_kind_p95_ms": {k: round(pct([x * 1000 for x in v], 95), 1) for k, v in sorted(m.by_kind.items())},
        "mean_ms": round(statistics.fmean(lat), 1) if lat else 0,
    }
