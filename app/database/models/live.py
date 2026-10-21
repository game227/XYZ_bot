from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import LiveLinkVisibility, LiveSessionStatus, str_enum


class LiveSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "live_sessions"
    __table_args__ = (Index("ix_live_sessions_status_starts", "status", "starts_at"),)

    course_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("courses.id", ondelete="SET NULL"), index=True
    )
    teacher_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teacher_accounts.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)  # ishtirok etish shartlari
    starts_at: Mapped[datetime]
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    join_url: Mapped[str | None] = mapped_column(String(1000))
    link_visibility: Mapped[LiveLinkVisibility] = mapped_column(
        str_enum(LiveLinkVisibility),
        default=LiveLinkVisibility.SUBSCRIBERS,
        server_default=LiveLinkVisibility.SUBSCRIBERS.value,
    )
    registration_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    status: Mapped[LiveSessionStatus] = mapped_column(
        str_enum(LiveSessionStatus),
        default=LiveSessionStatus.SCHEDULED,
        server_default=LiveSessionStatus.SCHEDULED.value,
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class LiveSessionRegistration(IdMixin, Base):
    __tablename__ = "live_session_registrations"
    __table_args__ = (UniqueConstraint("live_session_id", "user_id"),)

    live_session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("live_sessions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
