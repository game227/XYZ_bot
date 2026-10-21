import time

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.database.models import DeliveryStatus
from app.services import notifications

METHOD = SendMessage(chat_id=1, text="x")


class FakeMessage:
    message_id = 99


class FakeBot:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def send_message(self, chat_id: int, text: str) -> FakeMessage:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return FakeMessage()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(notifications.asyncio, "sleep", fake_sleep)
    return slept


async def test_sent():
    result = await notifications.send_one(FakeBot(), 1, "hi", notifications.RateLimiter(1000))
    assert result == notifications.SendResult(DeliveryStatus.SENT, 99)


async def test_retry_after_is_honoured(no_sleep):
    bot = FakeBot(TelegramRetryAfter(METHOD, "flood", retry_after=7))
    result = await notifications.send_one(bot, 1, "hi", notifications.RateLimiter(1000))
    assert result.status == DeliveryStatus.SENT and bot.calls == 2
    assert 8 in no_sleep


async def test_blocked_user():
    bot = FakeBot(TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user"))
    result = await notifications.send_one(bot, 1, "hi", notifications.RateLimiter(1000))
    assert result.status == DeliveryStatus.BLOCKED and bot.calls == 1


async def test_bad_request_is_not_retried():
    bot = FakeBot(TelegramBadRequest(METHOD, "Bad Request: chat not found"))
    result = await notifications.send_one(bot, 1, "hi", notifications.RateLimiter(1000))
    assert result.status == DeliveryStatus.FAILED and "chat not found" in (result.error or "")


async def test_retries_are_bounded():
    bot = FakeBot(*(TelegramRetryAfter(METHOD, "flood", retry_after=1) for _ in range(5)))
    result = await notifications.send_one(bot, 1, "hi", notifications.RateLimiter(1000))
    assert result.status == DeliveryStatus.FAILED and bot.calls == notifications.MAX_SEND_TRIES


async def test_rate_limiter_spacing(no_sleep):
    limiter = notifications.RateLimiter(10)
    start = time.monotonic()
    for _ in range(5):
        await limiter.wait()
    # 5 sends at 10/s: slots 0.1 s apart. Sleep is faked (the clock doesn't move), so the
    # requested delays grow by one interval per call.
    assert len(no_sleep) == 4
    assert all(abs(b - a - 0.1) < 0.01 for a, b in zip([0.0, *no_sleep], no_sleep, strict=False))
    assert time.monotonic() - start < 1


def test_render_title_is_escaped():
    n = notifications.Notification(title="<x>", body="<b>ok</b>")
    assert notifications.render(n) == "<b>&lt;x&gt;</b>\n\n<b>ok</b>"
