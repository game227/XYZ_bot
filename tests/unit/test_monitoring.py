"""Monitoring: metrics, heartbeats, bot-down alerts (TZ §18, §19) and the stats cache."""

import json
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import auth, monitoring, stats


class HashRedis:
    def __init__(self) -> None:
        self.kv: dict[str, object] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    async def hincrby(self, name: str, field: str, amount: int) -> int:
        h = self.hashes.setdefault(name, {})
        h[field] = h.get(field, 0) + amount
        return h[field]

    async def hgetall(self, name: str) -> dict[str, int]:
        return dict(self.hashes.get(name, {}))

    async def expire(self, name: str, seconds: int) -> bool:
        return True

    async def get(self, name: str) -> object:
        return self.kv.get(name)

    async def set(self, name: str, value: object, ex: int | None = None, nx: bool = False) -> object:
        if nx and name in self.kv:
            return None
        self.kv[name] = value
        return True

    async def delete(self, *names: str) -> int:
        return sum(1 for n in names if self.kv.pop(n, None) is not None)


@pytest.fixture
def redis(monkeypatch) -> HashRedis:
    r = HashRedis()
    monkeypatch.setattr(monitoring, "_redis", lambda: r)
    return r


async def test_middleware_counts_updates_and_errors(redis) -> None:
    mw = monitoring.MetricsMiddleware()

    async def handler(event, data):
        return "ok"

    assert await mw(handler, object(), {}) == "ok"
    assert await mw(handler, object(), {}) == "ok"
    await monitoring.count_error()
    report = await monitoring.health_report()
    assert (report.updates_1h, report.errors_1h, report.updates_24h, report.errors_24h) == (2, 1, 2, 1)


async def test_middleware_counts_even_when_handler_raises(redis) -> None:
    async def handler(event, data):
        raise RuntimeError

    with pytest.raises(RuntimeError):
        await monitoring.MetricsMiddleware()(handler, object(), {})
    assert (await monitoring.health_report()).updates_1h == 1


async def test_heartbeat_age(redis) -> None:
    assert await monitoring.last_beat("bot") is None
    redis.kv["xyz:hb:bot"] = int(time.time()) - 10
    assert 9 <= await monitoring.last_beat("bot") <= 11


async def test_bot_down_alert_once_per_outage(redis, monkeypatch) -> None:
    monkeypatch.setattr(monitoring, "get_settings", lambda: SimpleNamespace(superadmin_ids=[1]))
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await monitoring.check_bot_alive(bot) is False
    assert await monitoring.check_bot_alive(bot) is False
    assert bot.send_message.await_count == 1  # one alert for the whole outage
    await monitoring.beat("bot")
    assert await monitoring.check_bot_alive(bot) is True
    assert bot.send_message.await_count == 2  # "back online"
    assert await monitoring.check_bot_alive(bot) is True
    assert bot.send_message.await_count == 2


async def test_redis_down_never_breaks_handler(monkeypatch) -> None:
    def boom():
        raise ConnectionError

    monkeypatch.setattr(monitoring, "_redis", boom)

    async def handler(event, data):
        return 42

    assert await monitoring.MetricsMiddleware()(handler, object(), {}) == 42
    assert (await monitoring.health_report()).bot_beat is None


async def test_stats_report_served_from_cache(monkeypatch, redis) -> None:
    report = stats.StatsReport(generated_at="2026-10-24T10:00:00+00:00", users_total=7)
    redis.kv[stats.CACHE_KEY] = json.dumps(asdict(report))
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    cached = await stats.get_report(session=None)
    assert cached.users_total == 7
