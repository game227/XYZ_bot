from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import PaymentRequestStatus, SubscriptionStatus, str_enum


class SubscriptionPlan(IdMixin, TimestampMixin, Base):
    __tablename__ = "subscription_plans"

    name: Mapped[str] = mapped_column(String(255))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), default="UZS", server_default="UZS")
    duration_days: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class PaymentRequest(IdMixin, TimestampMixin, Base):
    """Manual payment review. Approval must be a conditional UPDATE ... WHERE status='pending'."""

    __tablename__ = "payment_requests"
    __table_args__ = (
        Index("ix_payment_requests_status_created", "status", "created_at"),
        # At most one pending request per user.
        Index(
            "uq_payment_requests_user_pending",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("subscription_plans.id"))
    # Snapshot of the plan at request time.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), default="UZS", server_default="UZS")
    duration_days: Mapped[int] = mapped_column(Integer)

    status: Mapped[PaymentRequestStatus] = mapped_column(
        str_enum(PaymentRequestStatus),
        default=PaymentRequestStatus.PENDING,
        server_default=PaymentRequestStatus.PENDING.value,
    )
    proof_kind: Mapped[str | None] = mapped_column(String(16))  # photo | document | text
    proof_file_id: Mapped[str | None] = mapped_column(String(255))  # Telegram file_id
    user_comment: Mapped[str | None] = mapped_column(Text)

    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None]
    decision_comment: Mapped[str | None] = mapped_column(Text)


class Subscription(IdMixin, TimestampMixin, Base):
    """Active iff status='active' AND starts_at <= now() < ends_at."""

    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_user_ends", "user_id", "ends_at"),)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    plan_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("subscription_plans.id"))
    # UNIQUE: one payment request can create at most one subscription.
    payment_request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("payment_requests.id", ondelete="SET NULL"), unique=True
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        str_enum(SubscriptionStatus),
        default=SubscriptionStatus.ACTIVE,
        server_default=SubscriptionStatus.ACTIVE.value,
    )
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime] = mapped_column(index=True)
    cancelled_at: Mapped[datetime | None]
    granted_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )

    reminder_3d_sent_at: Mapped[datetime | None]
    reminder_1d_sent_at: Mapped[datetime | None]
    expired_notified_at: Mapped[datetime | None]
