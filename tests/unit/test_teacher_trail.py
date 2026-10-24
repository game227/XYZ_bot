"""Teacher message trail: recording in teacher scope and purging (TZ §7.4–7.5)."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update

from app.services import teacher_sessions, teacher_trail

TEACHER_TG = 111
STUDENT_TG = 222


class SetRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.ttl: dict[str, int] = {}

    async def sadd(self, name: str, *values: str) -> int:
        self.sets.setdefault(name, set()).update(values)
        return len(values)

    async def expire(self, name: str, seconds: int) -> bool:
        self.ttl[name] = seconds
        return True

    async def smembers(self, name: str) -> set[str]:
        return set(self.sets.get(name, set()))

    async def delete(self, *names: str) -> int:
        return sum(1 for n in names if self.sets.pop(n, None) is not None)

    async def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        for name in list(self.sets):
            if name.startswith(prefix):
                yield name


@pytest.fixture
def redis(monkeypatch) -> SetRedis:
    r = SetRedis()
    monkeypatch.setattr(teacher_trail, "_redis", lambda: r)
    return r


def _message(chat_id: int, message_id: int) -> Message:
    return Message(
        message_id=message_id, date=int(time.time()), chat=Chat(id=chat_id, type="private"), text="x"
    )


def _send(chat_id: int, message_id: int):
    """A request middleware call for SendMessage answered with the given message id. Like
    aiogram's sessions, `make_request` returns the method's result itself, not a Response."""
    method = SendMessage(chat_id=chat_id, text="x")

    async def make_request(bot, m):
        return _message(chat_id, message_id)

    return make_request, method


async def _run_update(handler, incoming_chat: int = TEACHER_TG, incoming_id: int = 1) -> None:
    update = Update(update_id=1, message=_message(incoming_chat, incoming_id))
    await teacher_trail.TrailUpdateMiddleware()(lambda event, data: handler(), update, {})


async def test_records_only_teacher_chat_while_in_scope(redis) -> None:
    mw = teacher_trail.TrailRequestMiddleware()

    async def handler():
        make, method = _send(TEACHER_TG, 10)  # before mark: ignored
        await mw(make, None, method)
        teacher_trail.mark(7, TEACHER_TG)
        make, method = _send(TEACHER_TG, 11)
        await mw(make, None, method)
        make, method = _send(STUDENT_TG, 12)  # reply delivered to a student: never recorded
        await mw(make, None, method)

    await _run_update(handler, incoming_id=5)
    assert redis.sets[teacher_trail.key(7)] == {f"{TEACHER_TG}:11", f"{TEACHER_TG}:5"}
    assert redis.ttl[teacher_trail.key(7)] == teacher_trail.TRAIL_TTL_SECONDS


async def test_real_bot_request_chain(redis) -> None:
    """Through aiogram's own Bot -> session -> middleware chain (no hand-made Response)."""
    from tests.load.harness import DUMMY_TOKEN, FakeTelegramSession

    bot = Bot(DUMMY_TOKEN, session=FakeTelegramSession(latency_ms=(0, 0)))
    bot.session.middleware(teacher_trail.TrailRequestMiddleware())
    sent: list[Message] = []

    async def handler():
        teacher_trail.mark(7, TEACHER_TG)
        sent.append(await bot.send_message(TEACHER_TG, "chat"))
        await bot.send_message(STUDENT_TG, "reply to a student")

    await _run_update(handler, incoming_id=3)
    assert isinstance(sent[0], Message)
    assert redis.sets[teacher_trail.key(7)] == {f"{TEACHER_TG}:{sent[0].message_id}", f"{TEACHER_TG}:3"}


async def test_scope_does_not_leak_between_updates(redis) -> None:
    mw = teacher_trail.TrailRequestMiddleware()

    async def teacher_update():
        teacher_trail.mark(7, TEACHER_TG)

    async def plain_update():
        make, method = _send(TEACHER_TG, 20)
        await mw(make, None, method)

    await _run_update(teacher_update, incoming_id=1)
    await _run_update(plain_update, incoming_id=2)
    assert redis.sets[teacher_trail.key(7)] == {f"{TEACHER_TG}:1"}


async def test_unmark_stops_recording(redis) -> None:
    mw = teacher_trail.TrailRequestMiddleware()

    async def handler():
        teacher_trail.mark(7, TEACHER_TG)
        teacher_trail.unmark()
        make, method = _send(TEACHER_TG, 30)
        await mw(make, None, method)

    await _run_update(handler)
    assert teacher_trail.key(7) not in redis.sets


async def test_mark_outside_update_is_noop(redis) -> None:
    teacher_trail.mark(7, TEACHER_TG)  # worker / tests: no scope prepared
    make, method = _send(TEACHER_TG, 40)
    await teacher_trail.TrailRequestMiddleware()(make, None, method)
    assert redis.sets == {}


async def test_purge_deletes_in_batches_and_clears_key(redis) -> None:
    await redis.sadd(teacher_trail.key(7), *[f"{TEACHER_TG}:{i}" for i in range(1, 151)])
    bot = SimpleNamespace(delete_messages=AsyncMock(side_effect=[True, RuntimeError("too old")]))
    assert await teacher_trail.purge(bot, 7) == 150
    calls = bot.delete_messages.await_args_list
    assert [len(c.args[1]) for c in calls] == [100, 50]
    assert all(c.args[0] == TEACHER_TG for c in calls)
    assert teacher_trail.key(7) not in redis.sets


async def test_sweep_purges_only_ended_sessions(redis, monkeypatch) -> None:
    await redis.sadd(teacher_trail.key(7), f"{TEACHER_TG}:1")
    await redis.sadd(teacher_trail.key(8), f"{TEACHER_TG}:2")
    monkeypatch.setattr(teacher_sessions, "active_session_ids", AsyncMock(return_value={8}))
    bot = SimpleNamespace(delete_messages=AsyncMock())
    assert await teacher_trail.sweep(bot, session=None) == 1
    bot.delete_messages.assert_awaited_once_with(TEACHER_TG, [1])
    assert set(redis.sets) == {teacher_trail.key(8)}


async def test_redis_failure_never_breaks_sending(monkeypatch) -> None:
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(teacher_trail, "_redis", boom)
    mw = teacher_trail.TrailRequestMiddleware()

    async def handler():
        teacher_trail.mark(7, TEACHER_TG)
        make, method = _send(TEACHER_TG, 50)
        sent = await mw(make, None, method)
        assert sent.message_id == 50

    await _run_update(handler)
