"""Audit log of important actions (TZ §16). Never store secrets here."""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminAuditLog, User

logger = logging.getLogger(__name__)

_SECRET_MARKERS = ("password", "passwd", "token", "secret", "hash", "key", "otp", "code")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if any(m in str(k).lower() for m in _SECRET_MARKERS) else _sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def log_action(
    session: AsyncSession,
    action: str,
    *,
    actor: User | None = None,
    actor_telegram_id: int | None = None,
    entity_type: str | None = None,
    entity_id: object = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction (committed with the caller's changes).

    action: dotted name, e.g. "teacher.created", "teacher.login_failed",
    "teacher_session.revoked", "payment_request.approved", "lesson.updated".
    """
    session.add(
        AdminAuditLog(
            actor_user_id=actor.id if actor else None,
            actor_telegram_id=actor.telegram_id if actor else actor_telegram_id,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            details=_sanitize(details or {}),
        )
    )
    logger.info(
        "audit %s %s:%s by %s",
        action,
        entity_type,
        entity_id,
        actor.telegram_id if actor else actor_telegram_id,
    )
