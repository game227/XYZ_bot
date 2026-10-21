"""Callback data + small keyboard helpers for COMMERCE screens (prefix `cm_`)."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class CmPlanCb(CallbackData, prefix="cm_pl"):
    action: str  # list | view | buy
    plan_id: int = 0


class CmReqCb(CallbackData, prefix="cm_rq"):
    action: str  # cancel
    request_id: int


class CmAdmPlanCb(CallbackData, prefix="cm_apl"):
    action: str  # list | view | new | edit | toggle | grant
    plan_id: int = 0
    field: str = ""


class CmGrantCb(CallbackData, prefix="cm_gr"):
    action: str  # days | custom | cancelask | cancelsub
    user_id: int
    days: int = 0


class CmPayCb(CallbackData, prefix="cm_pay"):
    action: str  # list | view | approve | reject
    request_id: int = 0
    page: int = 0
    status: str = "pending"


class CmInboxCb(CallbackData, prefix="cm_in"):
    action: str  # page | toggle
    page: int = 0


class CmBcCb(CallbackData, prefix="cm_bc"):
    action: str  # menu | new | aud | course | send | cancel | view
    item_id: int = 0
    value: str = ""


class CmLiveCb(CallbackData, prefix="cm_lv"):
    action: str  # list | view | reg | unreg
    live_id: int = 0


class CmAdmLiveCb(CallbackData, prefix="cm_alv"):
    action: str  # list | past | new | view | status | edit | reg | vis | course | setcourse
    live_id: int = 0
    value: str = ""


class CmStatsCb(CallbackData, prefix="cm_st"):
    action: str  # refresh


def rows(*buttons: InlineKeyboardButton | None, width: int = 1) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for button in buttons:
        if button is not None:
            builder.add(button)
    builder.adjust(width)
    return builder.as_markup()


def btn(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def pager(
    make: "type[CallbackData] | object", page: int, total: int, per_page: int, **fields: object
) -> list[InlineKeyboardButton]:
    """◀️ / ▶️ buttons; `make(**fields, page=n)` builds the callback data."""
    buttons = []
    if page > 0:
        buttons.append(btn("◀️", make(page=page - 1, **fields)))  # type: ignore[operator]
    if (page + 1) * per_page < total:
        buttons.append(btn("▶️", make(page=page + 1, **fields)))  # type: ignore[operator]
    return buttons
