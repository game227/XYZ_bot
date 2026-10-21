"""Catch-all handlers. Included LAST by app.handlers.collect_routers()."""

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from app.database.models import User
from app.handlers.common.start import main_menu_for

router = Router(name="common.fallback")


@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer("Bu tugma eskirgan. /menu orqali qaytadan boshlang.", show_alert=False)


@router.message()
async def unknown_message(message: Message, user: User) -> None:
    await message.answer(
        "Buyruq tushunilmadi. Quyidagi menyudan foydalaning yoki /help ni bosing.",
        reply_markup=main_menu_for(user),
    )
