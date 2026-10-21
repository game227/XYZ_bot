import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user flood guard: at most `rate` updates per `per` seconds (sliding window).

    In-process only; good enough for a single polling process. Login brute-force
    protection is separate (Redis, services/auth.py).
    """

    def __init__(self, rate: int = 8, per: float = 4.0, max_tracked: int = 50_000) -> None:
        self.rate = rate
        self.per = per
        self.max_tracked = max_tracked
        self._hits: dict[int, list[float]] = {}
        self._warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        now = time.monotonic()
        hits = [t for t in self._hits.get(tg_user.id, []) if now - t < self.per]
        if len(hits) >= self.rate:
            self._hits[tg_user.id] = hits
            if now - self._warned.get(tg_user.id, 0) > self.per:
                self._warned[tg_user.id] = now
                text = "Juda tez so‘rov yuboryapsiz. Biroz kuting."
                if isinstance(event, CallbackQuery):
                    await event.answer(text)
                elif isinstance(event, Message):
                    await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer()
            return None

        hits.append(now)
        self._hits[tg_user.id] = hits
        if len(self._hits) > self.max_tracked:
            self._gc(now)
        return await handler(event, data)

    def _gc(self, now: float) -> None:
        self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.per}
        self._warned = {k: v for k, v in self._warned.items() if now - v < self.per}
