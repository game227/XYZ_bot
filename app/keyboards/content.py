"""Callback data and small keyboard helpers for CONTENT screens (prefix `ct_`).

Callback data is only a pointer: every handler re-checks access on the server.
"""

from collections.abc import Sequence

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.utils.text import truncate

TEXT_CHUNK = 3500

PAGE_SIZE = 10
BUTTON_TEXT_MAX = 60


class CtNav(CallbackData, prefix="ct_nav"):
    """Student catalog navigation. level: subjects | subject | course | module | topic."""

    level: str
    id: int = 0


class CtLes(CallbackData, prefix="ct_les"):
    """Student lesson actions. action: open | confirm | mat | quizzes."""

    action: str
    lesson_id: int
    media_id: int = 0


class CtList(CallbackData, prefix="ct_lst"):
    """Paged student lists. kind: free | video | quizzes | results."""

    kind: str
    page: int = 0


class CtQz(CallbackData, prefix="ct_qz"):
    """Student quiz flow. action: card | start | pick | toggle | submit | finish | result."""

    action: str
    quiz_id: int = 0
    attempt_id: int = 0
    question_id: int = 0
    option_id: int = 0


class CtEd(CallbackData, prefix="ct_ed"):
    """Content editor (admins + assigned teachers).

    action: root | courses | view | new | edit | toggle | free | up | down | del | delok |
            media | detach | addfile
    kind: subject | course | module | topic | lesson
    field: edited field code (t, d, s, b) or child kind for `new`
    """

    action: str
    kind: str = ""
    id: int = 0
    field: str = ""
    extra: int = 0


class CtQe(CallbackData, prefix="ct_qe"):
    """Quiz editor (admins with QUIZZES + teachers with can_manage_quizzes).

    action: courses | course | lesson | new | view | set | flag | cycle | qlist | qview | qadd |
            qtype | qdel | qdelok | del | delok
    """

    action: str
    id: int = 0
    arg: str = ""


def btn(text: str, callback_data: CallbackData | str) -> InlineKeyboardButton:
    data = callback_data if isinstance(callback_data, str) else callback_data.pack()
    return InlineKeyboardButton(text=truncate(text, BUTTON_TEXT_MAX), callback_data=data)


def markup(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def pager_row(
    page: int,
    total: int,
    make: type[CallbackData] | None = None,
    *,
    page_size: int = PAGE_SIZE,
    **fields: object,
) -> list[InlineKeyboardButton]:
    """◀️ 2/5 ▶️ row; empty when everything fits on one page."""
    pages = max(1, -(-total // page_size))
    if pages <= 1 or make is None:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(btn("◀️", make(page=page - 1, **fields)))
    row.append(btn(f"{page + 1}/{pages}", make(page=page, **fields)))
    if page < pages - 1:
        row.append(btn("▶️", make(page=page + 1, **fields)))
    return row


def active_mark(is_active: bool) -> str:
    return "" if is_active else " 🚫"


async def show(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Replace the callback's message in place (falls back to a new message)."""
    message = callback.message
    if isinstance(message, Message) and message.text is not None:
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    if message is not None:
        await callback.bot.send_message(message.chat.id, text, reply_markup=reply_markup)  # type: ignore[union-attr]


def chunks(text: str, size: int = TEXT_CHUNK) -> list[str]:
    """Split raw (unescaped) text into message-sized pieces, preferring line breaks."""
    text = text or ""
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts
