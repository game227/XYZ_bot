"""Adding and removing admins (TZ §3.1). The only superadmin comes from SUPERADMIN_IDS.

Admins use the bot like any other user and open the admin panel with the «🛠 Admin» button.
A new admin gets the permissions of the admin who adds them (never more), so an admin with
every permission can in turn add and remove admins.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, TeacherAccount, User, UserRole, UserStatus
from app.services.access import has_permission, is_superadmin, require_admin
from app.services.audit import log_action
from app.services.errors import AccessDenied, Conflict, InvalidInput, NotFound


def parse_telegram_id(text: str | None) -> int:
    value = (text or "").strip()
    if not value.isdigit() or not 5 <= len(value) <= 15:
        raise InvalidInput("Telegram user ID faqat raqamlardan iborat bo‘lishi kerak (masalan 123456789).")
    return int(value)


def grantable_permissions(actor: User) -> list[str]:
    return [p.value for p in AdminPermission if has_permission(actor, p)]


async def list_admins(session: AsyncSession) -> list[User]:
    stmt = select(User).where(User.role == UserRole.ADMIN).order_by(User.created_at)
    return list((await session.scalars(stmt)).all())


async def add_admin(session: AsyncSession, actor: User, telegram_id: int) -> tuple[User, bool]:
    """Make the user with this Telegram ID an admin. Returns (user, created).

    The user doesn't have to have started the bot yet: the row is created now and they see the
    «🛠 Admin» button on their first /start.
    """
    require_admin(actor, AdminPermission.ADMINS)
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
    created = user is None
    if user is None:
        user = User(telegram_id=telegram_id, role=UserRole.ADMIN)
        session.add(user)
    elif is_superadmin(user):
        raise Conflict("Bu foydalanuvchi superadmin.")
    elif user.role == UserRole.ADMIN:
        raise Conflict("Bu foydalanuvchi allaqachon admin.")
    elif user.status == UserStatus.BLOCKED:
        raise AccessDenied("Bu foydalanuvchi bloklangan. Avval blokdan chiqaring.")
    user.role = UserRole.ADMIN
    user.permissions = grantable_permissions(actor)
    await session.flush()
    await log_action(
        session,
        "user.admin_granted",
        actor=actor,
        entity_type="user",
        entity_id=user.id,
        details={"telegram_id": telegram_id, "permissions": user.permissions},
    )
    return user, created


async def remove_admin(session: AsyncSession, actor: User, user_id: int) -> User:
    require_admin(actor, AdminPermission.ADMINS)
    user = await session.get(User, user_id, with_for_update=True)
    if user is None or user.role != UserRole.ADMIN:
        raise NotFound("Bu foydalanuvchi admin emas.")
    if is_superadmin(user):
        raise AccessDenied("Superadminni olib tashlab bo‘lmaydi.")
    if user.id == actor.id:
        raise AccessDenied("O‘zingizni adminlikdan olib tashlay olmaysiz.")
    teacher_bound = await session.scalar(select(TeacherAccount.id).where(TeacherAccount.user_id == user.id))
    user.role = UserRole.TEACHER if teacher_bound is not None else UserRole.STUDENT
    user.permissions = []
    await session.flush()
    await log_action(
        session,
        "user.admin_revoked",
        actor=actor,
        entity_type="user",
        entity_id=user.id,
        details={"telegram_id": user.telegram_id},
    )
    return user
