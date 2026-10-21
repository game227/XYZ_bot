"""Central server-side permission checks (TZ §3, §15.4-5).

Hiding a button is never a security measure: every handler / endpoint that returns
protected data must call one of these checks (or use the filters below).
"""

from typing import Any

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import AdminPermission, User, UserRole, UserStatus
from app.services.errors import AccessDenied


def is_superadmin(user: User) -> bool:
    return user.telegram_id in get_settings().superadmin_ids


def is_admin(user: User) -> bool:
    return is_superadmin(user) or (user.role == UserRole.ADMIN and user.status == UserStatus.ACTIVE)


def has_permission(user: User, perm: AdminPermission | str) -> bool:
    if is_superadmin(user):
        return True
    return is_admin(user) and str(perm) in (user.permissions or [])


def require_admin(user: User, perm: AdminPermission | str | None = None) -> None:
    """Raise AccessDenied unless `user` is an admin (with `perm`, if given)."""
    if not is_admin(user):
        raise AccessDenied()
    if perm is not None and not has_permission(user, perm):
        raise AccessDenied("Bu amal uchun sizda vakolat yo‘q.")


async def admin_telegram_ids(session: AsyncSession, perm: AdminPermission | str | None = None) -> list[int]:
    """Telegram IDs of active admins (having `perm`, if given), superadmins included."""
    stmt = select(User.telegram_id, User.permissions).where(
        User.role == UserRole.ADMIN, User.status == UserStatus.ACTIVE
    )
    ids = set(get_settings().superadmin_ids)
    for telegram_id, permissions in (await session.execute(stmt)).all():
        if perm is None or str(perm) in (permissions or []):
            ids.add(telegram_id)
    return sorted(ids)


class AdminFilter(Filter):
    """aiogram filter: passes only for admins (with `perm`, if given).

    Usage: @router.message(AdminFilter(AdminPermission.CONTENT), F.text == ...)
    Non-admins fall through silently (no information leak); the handler itself
    should still call `require_admin` before mutating data reached via callbacks
    built for another admin.
    """

    def __init__(self, perm: AdminPermission | str | None = None) -> None:
        self.perm = perm

    async def __call__(self, event: TelegramObject, user: User | None = None, **_: Any) -> bool:
        if user is None:
            return False
        return is_admin(user) if self.perm is None else has_permission(user, self.perm)


class RoleFilter(Filter):
    def __init__(self, *roles: UserRole) -> None:
        self.roles = set(roles)

    async def __call__(self, event: Message | CallbackQuery, user: User | None = None, **_: Any) -> bool:
        return user is not None and user.role in self.roles
