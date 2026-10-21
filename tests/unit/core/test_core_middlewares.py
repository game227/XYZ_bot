from datetime import UTC, datetime
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser

from app.keyboards import menus
from app.middlewares.menu_reset import MenuResetMiddleware
from app.middlewares.throttling import ThrottlingMiddleware


def _message(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=5, type="private"),
        from_user=TgUser(id=5, is_bot=False, first_name="A"),
        text=text,
    )


async def _state_with(value: str | None) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=5, user_id=5))
    if value:
        await state.set_state(value)
    return state


async def _passthrough(event: Any, data: dict[str, Any]) -> dict[str, Any]:
    return data


async def test_menu_button_clears_pending_input() -> None:
    state = await _state_with("CoUserSearch:query")
    data = {"state": state, "raw_state": "CoUserSearch:query"}
    result = await MenuResetMiddleware()(_passthrough, _message(menus.BTN_COURSES), data)
    assert result["raw_state"] is None
    assert await state.get_state() is None


async def test_command_clears_pending_input() -> None:
    state = await _state_with("CoUserSearch:query")
    data = {"state": state, "raw_state": "CoUserSearch:query"}
    result = await MenuResetMiddleware()(_passthrough, _message("/menu"), data)
    assert result["raw_state"] is None


async def test_free_text_keeps_pending_input() -> None:
    state = await _state_with("CoUserSearch:query")
    data = {"state": state, "raw_state": "CoUserSearch:query"}
    result = await MenuResetMiddleware()(_passthrough, _message("Ali"), data)
    assert result["raw_state"] == "CoUserSearch:query"
    assert await state.get_state() == "CoUserSearch:query"


async def test_throttling_drops_flood() -> None:
    middleware = ThrottlingMiddleware(rate=3, per=60)
    calls = 0

    async def handler(event: Any, data: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1

    data = {"event_from_user": TgUser(id=7, is_bot=False, first_name="B")}
    # Non-Message events skip the warning reply, so a plain object is enough here.
    for _ in range(5):
        await middleware(handler, object(), data)  # type: ignore[arg-type]
    assert calls == 3
