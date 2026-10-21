from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import (
    DeliveryStatus,
    NotificationAudience,
    NotificationStatus,
    str_enum,
)


class Notification(IdMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_status_created", "status", "created_at"),)

    # "manual" | "sub_activated" | "sub_expiring_3d" | "new_lesson" | ... (owned by services/notifications.py)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    audience_type: Mapped[NotificationAudience] = mapped_column(str_enum(NotificationAudience))
    # e.g. {"role": "student"} | {"course_id": 5} | {"user_ids": [1, 2]}
    audience: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    # Makes automatic notifications idempotent, e.g. "subexp:{user_id}:{ends_at}:3d".
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus),
        default=NotificationStatus.DRAFT,
        server_default=NotificationStatus.DRAFT.value,
    )
    scheduled_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    total_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sent_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class NotificationDelivery(IdMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", "user_id"),
        Index("ix_notification_deliveries_status", "notification_id", "status"),
        Index("ix_notification_deliveries_user", "user_id", "created_at"),
    )

    notification_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("notifications.id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus), default=DeliveryStatus.PENDING, server_default=DeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    sent_at: Mapped[datetime | None]
