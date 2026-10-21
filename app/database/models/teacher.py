from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import TeacherSessionEndReason, str_enum


class TeacherAccount(IdMixin, TimestampMixin, Base):
    """Login credentials of a teacher. Created only by an admin.

    `user_id` is the Telegram user bound to this account (set on first successful login,
    resettable by an admin). Sessions are always bound to a concrete Telegram ID.
    """

    __tablename__ = "teacher_accounts"

    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), unique=True
    )
    login: Mapped[str] = mapped_column(String(64), unique=True)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    full_name: Mapped[str] = mapped_column(String(255))
    is_blocked: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    blocked_at: Mapped[datetime | None]

    password_changed_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]

    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class TeacherSession(IdMixin, Base):
    """Strict 24h session. Valid iff revoked_at IS NULL AND now() < expires_at.

    Activity never extends expires_at. Only sha256(token) is stored (hex, 64 chars).
    At most one active session per teacher: a new login ends older ones (REPLACED).
    """

    __tablename__ = "teacher_sessions"
    __table_args__ = (
        Index(
            "ix_teacher_sessions_tg_active",
            "telegram_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    teacher_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teacher_accounts.id", ondelete="CASCADE"), index=True
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    end_reason: Mapped[TeacherSessionEndReason | None] = mapped_column(str_enum(TeacherSessionEndReason))
    revoked_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class TeacherCourseAssignment(IdMixin, TimestampMixin, Base):
    """Teacher <-> course link with per-course rights (TZ `teacher_course_assignments`/`course_teachers`)."""

    __tablename__ = "teacher_course_assignments"
    __table_args__ = (UniqueConstraint("teacher_id", "course_id"),)

    teacher_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teacher_accounts.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    can_manage_content: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    can_manage_quizzes: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    can_view_students: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    can_view_results: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    can_chat: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
