"""Telegram Mini App authentication (initData HMAC check), shared by all web routers."""

from typing import Annotated

from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import User, UserStatus
from app.utils.time import utcnow
from app.web.deps import get_db


class InitDataError(Exception):
    pass


def verify_init_data(init_data: str, *, max_age_seconds: int | None = None) -> WebAppInitData:
    """Validate Mini App initData signature and freshness. Raises InitDataError."""
    settings = get_settings()
    try:
        data = safe_parse_webapp_init_data(settings.bot_token.get_secret_value(), init_data)
    except ValueError as exc:
        raise InitDataError("invalid signature") from exc
    if data.user is None:
        raise InitDataError("no user")
    max_age = max_age_seconds if max_age_seconds is not None else settings.webapp_initdata_max_age_seconds
    if (utcnow() - data.auth_date).total_seconds() > max_age:
        raise InitDataError("expired")
    return data


async def webapp_init_data(
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> WebAppInitData:
    """FastAPI dependency: verified initData from the `X-Telegram-Init-Data` header."""
    if not x_telegram_init_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Telegram orqali oching.")
    try:
        return verify_init_data(x_telegram_init_data)
    except InitDataError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessiya yaroqsiz. Botdan qayta oching.") from exc


async def webapp_user(
    init_data: Annotated[WebAppInitData, Depends(webapp_init_data)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI dependency: the registered, non-blocked bot user behind the Mini App request."""
    assert init_data.user is not None
    user = await session.scalar(select(User).where(User.telegram_id == init_data.user.id))
    if user is None or user.status == UserStatus.BLOCKED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Kirish taqiqlangan.")
    return user
