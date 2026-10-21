"""End-to-end bot flow harness: the real Dispatcher fed with fake Telegram updates.

- `RecordingSession` replaces the HTTP session: every Bot API call is recorded and answered
  locally with a plausible result. Nothing ever reaches api.telegram.org.
- One Dispatcher per test run (routers are module-level singletons and can be attached once);
  each test uses fresh Telegram ids, so FSM state never leaks between tests.
- Data is committed for real (the app's own sessionmaker points at TEST_DATABASE_URL) and all
  tables are truncated after each test (`committing_sessionmaker`).
"""

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.types import User as TgUser

import app.bot as app_bot
from app.config import get_settings
from app.middlewares.throttling import ThrottlingMiddleware
from app.services import notifications

BOT_ID = 123456789  # from the dummy BOT_TOKEN set in tests/conftest.py
_ids = itertools.count(1)


@dataclass
class Sent:
    """One recorded outgoing message (send* or edit*)."""

    method: str
    chat_id: int
    message_id: int
    text: str
    markup: Any
    is_photo: bool = False


class RecordingSession(AiohttpSession):
    """Fake Bot API session: records calls, never touches the network."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.sent: list[Sent] = []
        self._message_ids = itertools.count(1000)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - signature fixed by aiogram
    ) -> Any:
        self.calls.append(method)
        name = type(method).__name__
        if name.startswith(("Send", "Edit", "Copy")):
            chat_id = int(getattr(method, "chat_id", 0) or 0)
            message_id = int(getattr(method, "message_id", 0) or 0) or next(self._message_ids)
            text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
            markup = getattr(method, "reply_markup", None)
            is_photo = name == "SendPhoto"
            if name.startswith("Edit"):
                # Telegram replaces the markup on edits (none given = removed); text may be kept.
                previous = self.find_message(chat_id, message_id)
                if previous is not None:
                    text = text or previous.text
                    is_photo = previous.is_photo
            self.sent.append(Sent(name, chat_id, message_id, text, markup, is_photo))
            return Message(
                message_id=message_id,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                from_user=TgUser(id=BOT_ID, is_bot=True, first_name="XYZ"),
                text=None if is_photo else text,
                caption=text if is_photo else None,
            )
        if name == "GetMe":
            return TgUser(id=BOT_ID, is_bot=True, first_name="XYZ", username="xyz_test_bot")
        return True

    def find_message(self, chat_id: int, message_id: int) -> Sent | None:
        return next(
            (s for s in reversed(self.sent) if s.chat_id == chat_id and s.message_id == message_id), None
        )

    async def close(self) -> None:  # nothing to close
        return None


class NoThrottle(ThrottlingMiddleware):
    """Tests send updates faster than a human; flood protection is tested separately."""

    def __init__(self) -> None:
        super().__init__(rate=1_000_000, per=1.0)


@dataclass
class TgClient:
    """A fake Telegram user talking to the bot."""

    harness: "Harness"
    tg_id: int
    first_name: str = "Test"
    mark: int = 0  # index into harness.session.sent where the current step starts
    callback_answers: list[str] = field(default_factory=list)

    @property
    def user(self) -> TgUser:
        return TgUser(id=self.tg_id, is_bot=False, first_name=self.first_name, language_code="uz")

    def _chat(self) -> Chat:
        return Chat(id=self.tg_id, type="private")

    async def send(self, text: str) -> list[Sent]:
        message = Message(
            message_id=next(_ids), date=datetime.now(UTC), chat=self._chat(), from_user=self.user, text=text
        )
        return await self.harness.feed(self, Update(update_id=next(_ids), message=message))

    async def send_photo(
        self, file_id: str = "AgACAgIAAxkBAAIBproof", caption: str | None = None
    ) -> list[Sent]:
        photo = [PhotoSize(file_id=file_id, file_unique_id=f"u{next(_ids)}", width=800, height=600)]
        message = Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=self._chat(),
            from_user=self.user,
            photo=photo,
            caption=caption,
        )
        return await self.harness.feed(self, Update(update_id=next(_ids), message=message))

    def button(self, text_part: str, *, exact: bool = False) -> tuple[Sent, InlineKeyboardButton]:
        """Newest inline button (in a message to this user) whose text contains `text_part`."""
        for sent in reversed(self.harness.session.sent):
            if sent.chat_id != self.tg_id or not isinstance(sent.markup, InlineKeyboardMarkup):
                continue
            for row in sent.markup.inline_keyboard:
                for b in row:
                    if (b.text == text_part) if exact else (text_part in b.text):
                        return sent, b
        raise AssertionError(f"no inline button containing {text_part!r} for {self.tg_id}")

    def has_button(self, text_part: str) -> bool:
        try:
            self.button(text_part)
        except AssertionError:
            return False
        return True

    async def click(self, text_part: str) -> list[Sent]:
        sent, b = self.button(text_part)
        assert b.callback_data, f"button {b.text!r} is not a callback button"
        return await self.press(b.callback_data, on=sent)

    async def click_exact(self, text: str) -> list[Sent]:
        sent, b = self.button(text, exact=True)
        assert b.callback_data
        return await self.press(b.callback_data, on=sent)

    async def press(self, data: str, on: Sent | None = None) -> list[Sent]:
        """Send raw callback data (also used to forge stale/foreign callbacks)."""
        msg_kwargs: dict[str, Any] = {}
        if on is not None and on.is_photo:
            msg_kwargs = {
                "caption": on.text,
                "photo": [PhotoSize(file_id="x", file_unique_id="x", width=1, height=1)],
            }
        else:
            msg_kwargs = {"text": on.text if on else "…"}
        message = Message(
            message_id=on.message_id if on else next(_ids),
            date=datetime.now(UTC),
            chat=self._chat(),
            from_user=TgUser(id=BOT_ID, is_bot=True, first_name="XYZ"),
            reply_markup=on.markup if on and isinstance(on.markup, InlineKeyboardMarkup) else None,
            **msg_kwargs,
        )
        query = CallbackQuery(
            id=str(next(_ids)), from_user=self.user, chat_instance="e2e", data=data, message=message
        )
        return await self.harness.feed(self, Update(update_id=next(_ids), callback_query=query))

    # ---- inspection helpers

    def texts(self, replies: list[Sent]) -> str:
        return "\n---\n".join(s.text for s in replies)

    def last_alert(self) -> str:
        return self.callback_answers[-1] if self.callback_answers else ""

    def reply_keyboard(self, replies: list[Sent]) -> list[str]:
        for s in reversed(replies):
            if isinstance(s.markup, ReplyKeyboardMarkup):
                return [b.text for row in s.markup.keyboard for b in row]
        return []


class Harness:
    def __init__(self, dp: Dispatcher, bot: Bot, session: RecordingSession) -> None:
        self.dp, self.bot, self.session = dp, bot, session
        self._tg_ids = itertools.count(90_000_001)

    def client(self, tg_id: int | None = None, first_name: str = "Test") -> TgClient:
        return TgClient(self, tg_id or next(self._tg_ids), first_name)

    async def feed(self, client: TgClient, update: Update) -> list[Sent]:
        """Feed one update and return the messages sent/edited for it (any chat)."""
        start_sent, start_calls = len(self.session.sent), len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        for call in self.session.calls[start_calls:]:
            if type(call).__name__ == "AnswerCallbackQuery":
                client.callback_answers.append(getattr(call, "text", None) or "")
        return self.session.sent[start_sent:]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _dispatcher() -> AsyncIterator[tuple[Dispatcher, Bot, RecordingSession]]:
    mp = pytest.MonkeyPatch()
    mp.setattr(app_bot, "ThrottlingMiddleware", NoThrottle)
    dp = app_bot.create_dispatcher(get_settings(), storage=MemoryStorage())
    session = RecordingSession()
    bot = Bot(
        token=get_settings().bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    assert bot.id == BOT_ID
    yield dp, bot, session
    mp.undo()


@pytest_asyncio.fixture(loop_scope="session")
async def harness(_dispatcher, committing_sessionmaker, monkeypatch) -> AsyncIterator[Harness]:
    """Requires TEST_DATABASE_URL (committing_sessionmaker skips otherwise)."""

    async def no_enqueue(_: int) -> None:  # keep tests away from the arq queue
        return None

    monkeypatch.setattr(notifications, "enqueue_send", no_enqueue)
    dp, bot, session = _dispatcher
    yield Harness(dp, bot, session)


@pytest.fixture
def superadmin(harness: Harness, monkeypatch) -> TgClient:
    client = harness.client(first_name="Admin")
    monkeypatch.setattr(get_settings(), "superadmin_ids", [client.tg_id])
    return client
