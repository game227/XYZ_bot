"""Test helpers for CONTENT integration tests: build a minimal active content tree."""

import itertools
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AnswerOption,
    Course,
    Lesson,
    Module,
    Question,
    QuestionType,
    Quiz,
    Subject,
    Subscription,
    Topic,
    User,
    UserRole,
)
from app.utils.time import utcnow

_tg_ids = itertools.count(80_000_001)


async def make_tree(session: AsyncSession) -> Topic:
    subject = Subject(title="Matematika")
    session.add(subject)
    await session.flush()
    course = Course(subject_id=subject.id, title="Algebra")
    session.add(course)
    await session.flush()
    module = Module(course_id=course.id, title="1-modul")
    session.add(module)
    await session.flush()
    topic = Topic(module_id=module.id, title="Tenglamalar")
    session.add(topic)
    await session.flush()
    return topic


async def make_lessons(
    session: AsyncSession, topic: Topic, *, free: int = 0, paid: int = 0
) -> tuple[list[Lesson], list[Lesson]]:
    module = await session.get(Module, topic.module_id)
    assert module is not None
    made: dict[bool, list[Lesson]] = {True: [], False: []}
    for is_free, count in ((True, free), (False, paid)):
        for n in range(count):
            lesson = Lesson(
                topic_id=topic.id,
                course_id=module.course_id,
                title=f"{'Bepul' if is_free else 'Pullik'} dars {n + 1}",
                is_free=is_free,
                is_active=True,
                position=n,
            )
            session.add(lesson)
            made[is_free].append(lesson)
    await session.flush()
    return made[True], made[False]


async def make_student(session: AsyncSession) -> User:
    user = User(telegram_id=next(_tg_ids), first_name="O‘quvchi", role=UserRole.STUDENT)
    session.add(user)
    await session.flush()
    return user


async def subscribe(session: AsyncSession, user: User, *, days: int = 30) -> Subscription:
    now = utcnow()
    sub = Subscription(
        user_id=user.id, starts_at=now - timedelta(minutes=1), ends_at=now + timedelta(days=days)
    )
    session.add(sub)
    await session.flush()
    return sub


async def make_quiz(
    session: AsyncSession, lesson: Lesson, *, questions: int = 3, **settings: object
) -> tuple[Quiz, list[Question]]:
    """Single-choice questions, 1 point each; option text "to‘g‘ri" is correct, "xato" is wrong."""
    quiz = Quiz(lesson_id=lesson.id, title="Nazorat testi", is_active=True, **settings)
    session.add(quiz)
    await session.flush()
    made = []
    for n in range(questions):
        question = Question(
            quiz_id=quiz.id, type=QuestionType.SINGLE, text=f"{n + 1}-savol", position=n, points=1
        )
        session.add(question)
        await session.flush()
        session.add_all(
            [
                AnswerOption(question_id=question.id, text="to‘g‘ri", is_correct=True, position=1),
                AnswerOption(question_id=question.id, text="xato", is_correct=False, position=2),
            ]
        )
        made.append(question)
    await session.flush()
    return quiz, made
