from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import User, UserRole, UserSettings, UserStatus
from app.utils.time import utcnow

LAST_SEEN_RESOLUTION = timedelta(minutes=5)


async def upsert_user(session: AsyncSession, tg_user: TgUser) -> User:
    """Create the user on first contact and keep Telegram profile fields fresh."""
    now = utcnow()
    stmt = (
        insert(User)
        .values(
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code,
            last_seen_at=now,
        )
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
        .returning(User.id)
    )
    created_id = await session.scalar(stmt)
    if created_id is not None:
        await session.execute(insert(UserSettings).values(user_id=created_id).on_conflict_do_nothing())

    user = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
    assert user is not None

    if created_id is None:
        if (user.username, user.first_name, user.last_name) != (
            tg_user.username,
            tg_user.first_name,
            tg_user.last_name,
        ):
            user.username = tg_user.username
            user.first_name = tg_user.first_name
            user.last_name = tg_user.last_name
        if user.last_seen_at is None or now - user.last_seen_at > LAST_SEEN_RESOLUTION:
            user.last_seen_at = now
        if user.bot_blocked_at is not None:
            user.bot_blocked_at = None

    if tg_user.id in get_settings().superadmin_ids and user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
    return user


class UserMiddleware(BaseMiddleware):
    """Injects `user: User` (registered on first contact). Blocked users are stopped here."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        user = await upsert_user(session, tg_user)
        data["user"] = user

        if user.status == UserStatus.BLOCKED and tg_user.id not in get_settings().superadmin_ids:
            text = "Hisobingiz bloklangan. Savollar bo‘lsa, administratorga murojaat qiling."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return None
        return await handler(event, data)
