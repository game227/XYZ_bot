"""AUTH keyboards and callback data: teacher login / cabinet, admin teachers & sessions, chat."""

from collections.abc import Callable
from urllib.parse import urlencode

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config import get_settings


class AuCabinetCb(CallbackData, prefix="au_c"):
    action: str  # courses | course | students | student | results
    course_id: int = 0
    user_id: int = 0
    page: int = 0


class AuTeacherCb(CallbackData, prefix="au_t"):
    action: str  # list | view | new | block | unblock | reset_pw | reset_bind | courses | cancel
    teacher_id: int = 0
    page: int = 0
    confirm: int = 0


class AuAssignCb(CallbackData, prefix="au_as"):
    action: str  # toggle | rights | right
    teacher_id: int
    course_id: int
    right: str = ""
    page: int = 0


class AuSessionCb(CallbackData, prefix="au_s"):
    action: str  # list | view | revoke
    session_id: int = 0
    page: int = 0
    confirm: int = 0


class AuChatCb(CallbackData, prefix="au_ch"):
    action: str  # start | to | reply | cancel
    course_id: int = 0
    teacher_id: int = 0
    thread_id: int = 0


def is_https_webapp() -> bool:
    """Telegram accepts Mini App buttons only with HTTPS URLs."""
    return get_settings().web_base_url.startswith("https://")


def teacher_app_url(view: str | None = None, **params: object) -> str:
    query = {k: v for k, v in params.items() if v is not None}
    if view:
        query["view"] = view
    base = get_settings().web_base_url.rstrip("/") + "/teacher/"
    return f"{base}?{urlencode(query)}" if query else base


def teacher_app_button(text: str, view: str | None = None, **params: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=teacher_app_url(view, **params)))


def login_keyboard() -> InlineKeyboardMarkup | None:
    if not is_https_webapp():
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[teacher_app_button("🔐 Kirish oynasini ochish", "login")]])


def chats_keyboard(thread_id: int | None = None) -> InlineKeyboardMarkup | None:
    if not is_https_webapp():
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[teacher_app_button("💬 Chatlarni ochish", "chats", thread=thread_id)]]
    )


def rows(buttons: list[InlineKeyboardButton], per_row: int = 1) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def pager_row(page: int, has_next: bool, make: Callable[[int], str]) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=make(page - 1)))
    if has_next:
        row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=make(page + 1)))
    return row
