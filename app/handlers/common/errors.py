"""Global error handler: expected ServiceErrors become friendly replies; everything else
is logged, the user sees a generic message without technical details (TZ §15.15) and the
superadmins get a short alert (TZ §19)."""

import logging
import time

from aiogram import Bot, Router
from aiogram.types import ErrorEvent

from app.config import get_settings
from app.services import monitoring
from app.services.errors import ServiceError
from app.utils.text import esc

logger = logging.getLogger(__name__)

router = Router(name="common.errors")

GENERIC_ERROR = "Kutilmagan xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko‘ring."

# At most one alert per exception type per interval, so an error loop can't flood the admins.
ALERT_INTERVAL_SECONDS = 300
_last_alert: dict[str, float] = {}


async def _alert_superadmins(bot: Bot, exc: BaseException, update_id: int) -> None:
    """Only the exception class goes out: messages may contain user data or secrets."""
    kind = type(exc).__name__
    now = time.monotonic()
    if kind in _last_alert and now - _last_alert[kind] < ALERT_INTERVAL_SECONDS:
        return
    _last_alert[kind] = now
    text = (
        f"⚠️ Botda kutilmagan xatolik: <code>{esc(kind)}</code> (update {update_id}).\n"
        "Tafsilotlar server loglarida."
    )
    for admin_id in get_settings().superadmin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 - alerting must never raise
            logger.warning("Failed to deliver error alert to %s", admin_id, exc_info=True)


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    exc = event.exception
    if isinstance(exc, ServiceError):
        text = exc.user_message
    else:
        logger.exception("Unhandled error while processing update %s", event.update.update_id, exc_info=exc)
        text = GENERIC_ERROR
        await monitoring.count_error()
        await _alert_superadmins(bot, exc, event.update.update_id)

    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.answer(text)
    except Exception:  # noqa: BLE001 - never let the error handler raise
        logger.warning("Failed to deliver error message", exc_info=True)
    return True
