from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """Injects `session: AsyncSession` into handlers.

    Commits after the handler returns, rolls back if it raises. Handlers/services may
    commit earlier themselves (e.g. to release row locks before slow Telegram calls).
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return result
