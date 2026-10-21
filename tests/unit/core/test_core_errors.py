from typing import Any

import pytest

from app.config import get_settings
from app.handlers.common import errors


class _RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _reset_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(errors, "_last_alert", {})
    monkeypatch.setattr(get_settings(), "superadmin_ids", [111, 222])


async def test_alert_goes_to_every_superadmin_without_exception_text() -> None:
    bot = _RecordingBot()
    await errors._alert_superadmins(bot, RuntimeError("password=hunter2 user data"), 42)  # type: ignore[arg-type]
    assert [chat_id for chat_id, _ in bot.sent] == [111, 222]
    text = bot.sent[0][1]
    assert "RuntimeError" in text and "42" in text
    assert "hunter2" not in text and "user data" not in text


async def test_repeated_errors_are_rate_limited_per_type() -> None:
    bot = _RecordingBot()
    for _ in range(5):
        await errors._alert_superadmins(bot, RuntimeError("x"), 1)  # type: ignore[arg-type]
    await errors._alert_superadmins(bot, KeyError("y"), 2)  # type: ignore[arg-type]
    kinds = [text.split("<code>")[1].split("</code>")[0] for _, text in bot.sent]
    assert kinds == ["RuntimeError", "RuntimeError", "KeyError", "KeyError"]
