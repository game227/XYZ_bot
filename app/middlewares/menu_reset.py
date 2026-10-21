from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.keyboards.menus import MENU_BUTTONS


class MenuResetMiddleware(BaseMiddleware):
    """A main-menu button or any /command cancels the current FSM input flow.

    Runs before filters, so a handler waiting for free text (search query, new title...)
    never mistakes "📚 Fanlar va kurslar" or "/menu" for user input.
    """

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        text = event.text or ""
        state: FSMContext | None = data.get("state")
        if state is not None and data.get("raw_state") and (text in MENU_BUTTONS or text.startswith("/")):
            await state.clear()
            data["raw_state"] = None
        return await handler(event, data)
