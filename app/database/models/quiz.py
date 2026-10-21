from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin
from app.database.models.enums import (
    AttemptStatus,
    QuestionType,
    QuizAccessMode,
    QuizResultVisibility,
    str_enum,
)


class Quiz(IdMixin, TimestampMixin, Base):
    __tablename__ = "quizzes"

    lesson_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lessons.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # TZ §8.2 "bepul yoki pullik kirish": INHERIT follows the lesson.
    access_mode: Mapped[QuizAccessMode] = mapped_column(
        str_enum(QuizAccessMode), default=QuizAccessMode.INHERIT, server_default=QuizAccessMode.INHERIT.value
    )
    pass_percent: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    time_limit_seconds: Mapped[int | None] = mapped_column(Integer)
    max_attempts: Mapped[int | None] = mapped_column(Integer)  # NULL = unlimited
    shuffle_questions: Mapped[bool] = mapped_column(default=False, server_default=sa_text("false"))
    shuffle_options: Mapped[bool] = mapped_column(default=False, server_default=sa_text("false"))
    partial_credit: Mapped[bool] = mapped_column(default=False, server_default=sa_text("false"))
    result_visibility: Mapped[QuizResultVisibility] = mapped_column(
        str_enum(QuizResultVisibility, length=40),
        default=QuizResultVisibility.SCORE_ONLY,
        server_default=QuizResultVisibility.SCORE_ONLY.value,
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=sa_text("true"))
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class Question(IdMixin, TimestampMixin, Base):
    __tablename__ = "questions"

    quiz_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    type: Mapped[QuestionType] = mapped_column(str_enum(QuestionType))
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    points: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    explanation: Mapped[str | None] = mapped_column(Text)


class AnswerOption(IdMixin, Base):
    """For TEXT questions the is_correct options are the hidden accepted answers."""

    __tablename__ = "answer_options"

    question_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(String(1000))
    is_correct: Mapped[bool] = mapped_column(default=False, server_default=sa_text("false"))
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class QuizAttempt(IdMixin, Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (
        UniqueConstraint("quiz_id", "user_id", "attempt_no"),
        # At most one attempt in progress per user and quiz.
        Index(
            "uq_quiz_attempts_in_progress",
            "quiz_id",
            "user_id",
            unique=True,
            postgresql_where=sa_text("status = 'in_progress'"),
        ),
        Index("ix_quiz_attempts_user_finished", "user_id", "finished_at"),
    )

    quiz_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[AttemptStatus] = mapped_column(
        str_enum(AttemptStatus),
        default=AttemptStatus.IN_PROGRESS,
        server_default=AttemptStatus.IN_PROGRESS.value,
    )
    started_at: Mapped[datetime] = mapped_column(server_default=sa_text("now()"))
    deadline_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # Question ids in display order; option_order maps str(question_id) -> [option ids].
    question_order: Mapped[list[int]] = mapped_column(JSONB)
    option_order: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    current_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    max_score: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    passed: Mapped[bool | None]


class AttemptAnswer(IdMixin, Base):
    """UNIQUE(attempt_id, question_id) makes double-tapped answer callbacks idempotent."""

    __tablename__ = "attempt_answers"
    __table_args__ = (UniqueConstraint("attempt_id", "question_id"),)

    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("questions.id", ondelete="CASCADE"))
    selected_option_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    text_answer: Mapped[str | None] = mapped_column(Text)
    is_correct: Mapped[bool | None]
    points_awarded: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    answered_at: Mapped[datetime] = mapped_column(server_default=sa_text("now()"))
