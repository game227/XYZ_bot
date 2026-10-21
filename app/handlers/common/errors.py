"""Global error handler: expected ServiceErrors become friendly replies; everything else
is logged and the user sees a generic message without technical details (TZ §15.15)."""

import logging

from aiogram import Router
from aiogram.types import ErrorEvent

from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

router = Router(name="common.errors")

GENERIC_ERROR = "Kutilmagan xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko‘ring."


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if isinstance(exc, ServiceError):
        text = exc.user_message
    else:
        logger.exception("Unhandled error while processing update %s", event.update.update_id, exc_info=exc)
        text = GENERIC_ERROR

    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.answer(text)
    except Exception:  # noqa: BLE001 - never let the error handler raise
        logger.warning("Failed to deliver error message", exc_info=True)
    return True
