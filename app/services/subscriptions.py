"""Subscriptions & payment requests. OWNER: tg-xyz-cd.

d6 created this file with the two lookups below so other modules can depend on them
from day one; keep their signatures stable.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, SubscriptionStatus
from app.utils.time import utcnow


async def get_active_subscription(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> Subscription | None:
    """The subscription covering `now` (latest ending first), or None. `user_id` is users.id."""
    now = now or utcnow()
    stmt = (
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.starts_at <= now,
            Subscription.ends_at > now,
        )
        .order_by(Subscription.ends_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def has_active_subscription(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> bool:
    return await get_active_subscription(session, user_id, now=now) is not None
