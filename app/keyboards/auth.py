"""AUTH keyboards and callback data: teacher login / cabinet, admin teachers & sessions, chat."""

from collections.abc import Callable

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton


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


def rows(buttons: list[InlineKeyboardButton], per_row: int = 1) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def pager_row(page: int, has_next: bool, make: Callable[[int], str]) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=make(page - 1)))
    if has_next:
        row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=make(page + 1)))
    return row
