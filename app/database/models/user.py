from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import UserRole, UserStatus, str_enum


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_created_at", "created_at"),)

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(32))

    role: Mapped[UserRole] = mapped_column(
        str_enum(UserRole), default=UserRole.STUDENT, server_default=UserRole.STUDENT.value, index=True
    )
    status: Mapped[UserStatus] = mapped_column(
        str_enum(UserStatus), default=UserStatus.ACTIVE, server_default=UserStatus.ACTIVE.value
    )
    # Admin permission names (AdminPermission values). Ignored for non-admins.
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, server_default=text("'{}'")
    )
    # Set when Telegram reports the user blocked the bot; cleared on their next update.
    bot_blocked_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None] = mapped_column(index=True)

    settings: Mapped["UserSettings | None"] = relationship(back_populates="user", lazy="raise")

    @property
    def full_name(self) -> str:
        parts = [self.first_name or "", self.last_name or ""]
        return " ".join(p for p in parts if p).strip() or (self.username or str(self.telegram_id))

    def __repr__(self) -> str:
        return f"User(id={self.id}, telegram_id={self.telegram_id}, role={self.role})"


class UserSettings(TimestampMixin, Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    notifications_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tashkent", server_default="Asia/Tashkent")
    language: Mapped[str] = mapped_column(String(8), default="uz", server_default="uz")

    user: Mapped[User] = relationship(back_populates="settings", lazy="raise")
