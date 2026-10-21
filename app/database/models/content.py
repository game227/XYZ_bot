from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import MediaKind, MediaStatus, str_enum


class Subject(IdMixin, TimestampMixin, Base):
    __tablename__ = "subjects"

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class Course(IdMixin, TimestampMixin, Base):
    __tablename__ = "courses"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class Module(IdMixin, TimestampMixin, Base):
    __tablename__ = "modules"

    course_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class Topic(IdMixin, TimestampMixin, Base):
    __tablename__ = "topics"

    module_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("modules.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class Lesson(IdMixin, TimestampMixin, Base):
    __tablename__ = "lessons"
    __table_args__ = (Index("ix_lessons_free_active", "is_free", "is_active"),)

    topic_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    # Denormalized for cheap access checks (teacher assignment, course notifications).
    course_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    short_description: Mapped[str | None] = mapped_column(String(1000))
    body_text: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_free: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    published_at: Mapped[datetime | None]
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class MediaFile(IdMixin, TimestampMixin, Base):
    """Metadata only; bytes live in S3. Protected files are never exposed via public URLs."""

    __tablename__ = "media_files"

    kind: Mapped[MediaKind] = mapped_column(str_enum(MediaKind))
    status: Mapped[MediaStatus] = mapped_column(
        str_enum(MediaStatus), default=MediaStatus.UPLOADING, server_default=MediaStatus.UPLOADING.value
    )
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    # For videos after transcoding: S3 prefix of the HLS rendition + AES-128 segment key.
    hls_prefix: Mapped[str | None] = mapped_column(String(512))
    hls_aes_key: Mapped[bytes | None] = mapped_column(LargeBinary(16))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    is_protected: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    # Optional cached Telegram file_id for non-protected documents/images.
    telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class LessonMedia(IdMixin, Base):
    __tablename__ = "lesson_media"
    __table_args__ = (UniqueConstraint("lesson_id", "media_id"),)

    lesson_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lessons.id", ondelete="CASCADE"), index=True
    )
    media_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    caption: Mapped[str | None] = mapped_column(String(1000))


class CourseEnrollment(IdMixin, Base):
    """User <-> course link, created (ON CONFLICT DO NOTHING) when a student first opens
    any lesson of the course. Used for course-audience notifications and stats."""

    __tablename__ = "course_enrollments"
    __table_args__ = (UniqueConstraint("user_id", "course_id"),)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    course_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class LessonAccess(IdMixin, Base):
    """One row per (user, lesson) that was ever opened; written for every open (progress stats).

    `consumed_free_slot` is true only when the lesson was opened by a non-subscriber
    through the free allowance; the free limit counts only these rows.
    """

    __tablename__ = "lesson_accesses"
    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id"),
        Index("ix_lesson_accesses_user_free", "user_id", postgresql_where=text("consumed_free_slot")),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    lesson_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lessons.id", ondelete="CASCADE"), index=True
    )
    consumed_free_slot: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    first_opened_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_opened_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    open_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
