from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin
from app.database.models.enums import ChatSenderRole, str_enum


class TeacherChatThread(IdMixin, Base):
    """Bot-internal teacher<->student chat. Teachers read it only with a valid session."""

    __tablename__ = "teacher_chat_threads"
    __table_args__ = (
        UniqueConstraint("teacher_id", "student_user_id", "course_id", postgresql_nulls_not_distinct=True),
    )

    teacher_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teacher_accounts.id", ondelete="CASCADE"), index=True
    )
    student_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("courses.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_message_at: Mapped[datetime | None]
    is_closed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class TeacherChatMessage(IdMixin, Base):
    __tablename__ = "teacher_chat_messages"
    __table_args__ = (Index("ix_teacher_chat_messages_thread_id", "thread_id", "id"),)

    thread_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teacher_chat_threads.id", ondelete="CASCADE")
    )
    sender_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    sender_role: Mapped[ChatSenderRole] = mapped_column(str_enum(ChatSenderRole))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    read_at: Mapped[datetime | None]
