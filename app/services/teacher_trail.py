"""Teacher message trail (TZ §7.4–7.5). OWNER: AUTH.

The bot shows chats, students and results to a teacher as ordinary Telegram messages, and
those would stay in the teacher's chat history after the session ends. So everything the bot
shows a teacher during a session is deleted from the chat once that session is over:

- `require_teacher` calls `mark()`, which flags the current update as "teacher scope" for
  that session (a context variable prepared by `TrailUpdateMiddleware`).
- `TrailRequestMiddleware` (on the Bot session) records every message the bot sends to or
  edits in that teacher's chat while the flag is set. `TrailUpdateMiddleware` also records
  the teacher's own incoming messages handled in scope (chat replies and the like).
  Messages to other chats (e.g. a reply delivered to a student) are never recorded.
- Ids live in Redis, a set `xyz:tmsg:<session_id>` of "chat_id:message_id", for 48 h: the
  window in which Telegram lets a bot delete messages in a private chat.
- `purge()` runs right after /logout and an admin revoke; `sweep()` (worker, every minute)
  purges every session that is no longer valid (expired, replaced, blocked teacher...).

All of this is best effort: a Redis or Telegram failure never breaks the request itself.
"""

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import Message, MessageId, TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.time import utcnow

logger = logging.getLogger(__name__)

KEY_PREFIX = "xyz:tmsg:"
TRAIL_TTL_SECONDS = 48 * 3600
DELETE_BATCH = 100  # Bot API deleteMessages limit


@dataclass(slots=True)
class _Scope:
    session_id: int | None = None
    telegram_id: int | None = None


_scope: ContextVar[_Scope | None] = ContextVar("teacher_trail_scope", default=None)


def _redis():
    from app.services.auth import get_redis

    return get_redis()


def key(session_id: int) -> str:
    return f"{KEY_PREFIX}{session_id}"


def mark(session_id: int, telegram_id: int) -> None:
    """Flag the current update as teacher scope (no-op outside a bot update, e.g. worker)."""
    scope = _scope.get()
    if scope is not None:
        scope.session_id = session_id
        scope.telegram_id = telegram_id


def unmark() -> None:
    """Stop recording for the rest of the update (e.g. the "logged out" reply after /logout)."""
    scope = _scope.get()
    if scope is not None:
        scope.session_id = scope.telegram_id = None


async def record(session_id: int, chat_id: int, message_ids: list[int]) -> None:
    if not message_ids:
        return
    try:
        redis = _redis()
        k = key(session_id)
        await redis.sadd(k, *[f"{chat_id}:{mid}" for mid in message_ids])
        await redis.expire(k, TRAIL_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - never break the request
        logger.warning("teacher trail record failed: %s", type(exc).__name__)


def _message_ids(result: Any) -> list[int]:
    if isinstance(result, Message | MessageId):
        return [result.message_id]
    if isinstance(result, list):
        return [m.message_id for m in result if isinstance(m, Message | MessageId)]
    return []


class TrailRequestMiddleware(BaseRequestMiddleware):
    """Records messages the bot sends/edits in the teacher's own chat during teacher scope."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        # Despite the annotation, aiogram passes the method's result itself here (Message,
        # MessageId, bool...): AiohttpSession.make_request returns `response.result`.
        result = await make_request(bot, method)
        scope = _scope.get()
        if scope is not None and scope.session_id is not None:
            chat_id = getattr(method, "chat_id", None)
            if chat_id == scope.telegram_id:
                await record(scope.session_id, chat_id, _message_ids(result))
        return result


class TrailUpdateMiddleware(BaseMiddleware):
    """Opens a fresh scope per update; records the teacher's incoming message if in scope."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        scope = _Scope()
        token = _scope.set(scope)
        try:
            return await handler(event, data)
        finally:
            _scope.reset(token)
            message = event.message if isinstance(event, Update) else None
            if scope.session_id is not None and message is not None and message.chat.id == scope.telegram_id:
                await record(scope.session_id, message.chat.id, [message.message_id])


async def purge(bot: Bot, session_id: int) -> int:
    """Delete every recorded message of the session from Telegram. Returns how many ids."""
    try:
        redis = _redis()
        members = await redis.smembers(key(session_id))
        await redis.delete(key(session_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("teacher trail purge failed: %s", type(exc).__name__)
        return 0
    by_chat: dict[int, list[int]] = {}
    for item in members:
        chat_id, _, mid = str(item).partition(":")
        by_chat.setdefault(int(chat_id), []).append(int(mid))
    for chat_id, ids in by_chat.items():
        ids.sort()
        for i in range(0, len(ids), DELETE_BATCH):
            try:
                await bot.delete_messages(chat_id, ids[i : i + DELETE_BATCH])
            except Exception as exc:  # noqa: BLE001 - too old / already deleted / bot blocked
                logger.info("teacher trail delete failed: %s", type(exc).__name__)
    return sum(len(ids) for ids in by_chat.values())


async def sweep(bot: Bot, session: AsyncSession, *, now: datetime | None = None) -> int:
    """Purge the trail of every session that is no longer valid. Returns sessions purged."""
    from app.services.teacher_sessions import active_session_ids

    try:
        ids = [int(k.removeprefix(KEY_PREFIX)) async for k in _redis().scan_iter(match=f"{KEY_PREFIX}*")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("teacher trail sweep failed: %s", type(exc).__name__)
        return 0
    if not ids:
        return 0
    active = await active_session_ids(session, ids, now=now or utcnow())
    ended = [sid for sid in ids if sid not in active]
    for sid in ended:
        await purge(bot, sid)
    return len(ended)
