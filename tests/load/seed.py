"""Seed the dedicated load-test database (never the dev/prod DB).

Catalog: 3 subjects x 2 courses x 2 modules x 2 topics x 4 lessons = 96 lessons; the first
lesson of every topic in each subject's first course is free (12 free lessons); every second
lesson has a 5-question single-choice quiz (48 quizzes). Users: 10,000, of them 30% with an
active subscription.
"""

import random
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import asyncpg
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.database.models import (
    AnswerOption,
    Base,
    Course,
    Lesson,
    Module,
    Question,
    QuestionType,
    Quiz,
    Subject,
    Subscription,
    SubscriptionPlan,
    Topic,
    User,
    UserSettings,
)
from app.utils.time import utcnow

TELEGRAM_ID_BASE = 7_000_000_000


@dataclass
class LessonInfo:
    id: int
    subject_id: int
    course_id: int
    module_id: int
    topic_id: int
    is_free: bool
    quiz_id: int | None


@dataclass
class Catalog:
    lessons: list[LessonInfo] = field(default_factory=list)
    subscriber_tg: set[int] = field(default_factory=set)
    all_tg: list[int] = field(default_factory=list)

    @property
    def free(self) -> list[LessonInfo]:
        return [x for x in self.lessons if x.is_free]

    @property
    def paid(self) -> list[LessonInfo]:
        return [x for x in self.lessons if not x.is_free]


async def ensure_database(dsn_sqlalchemy: str) -> None:
    """CREATE DATABASE <name> if missing (connects to the `postgres` maintenance DB)."""
    dsn = dsn_sqlalchemy.replace("postgresql+asyncpg://", "postgresql://")
    base, _, name = dsn.rpartition("/")
    if name in {"xyz", "postgres"} or not name.startswith("xyz_load"):
        raise SystemExit(f"refusing to use database {name!r}: load tests need a dedicated xyz_load* DB")
    conn = await asyncpg.connect(base + "/postgres")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def reset_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def seed(engine: AsyncEngine, *, users: int = 10_000, subscriber_share: float = 0.3) -> Catalog:
    rng = random.Random(42)
    catalog = Catalog()
    async with AsyncSession(engine, expire_on_commit=False) as s:
        for si in range(3):
            subject = Subject(title=f"Fan {si + 1}", description="Yuklama testi uchun fan", position=si)
            s.add(subject)
            await s.flush()
            for ci in range(2):
                course = Course(subject_id=subject.id, title=f"Kurs {si + 1}.{ci + 1}", position=ci)
                s.add(course)
                await s.flush()
                for mi in range(2):
                    module = Module(course_id=course.id, title=f"Modul {mi + 1}", position=mi)
                    s.add(module)
                    await s.flush()
                    for ti in range(2):
                        topic = Topic(module_id=module.id, title=f"Mavzu {ti + 1}", position=ti)
                        s.add(topic)
                        await s.flush()
                        for li in range(4):
                            is_free = ci == 0 and li == 0
                            lesson = Lesson(
                                topic_id=topic.id,
                                course_id=course.id,
                                title=f"Dars {si + 1}.{ci + 1}.{mi + 1}.{ti + 1}.{li + 1}",
                                short_description="Qisqa tavsif",
                                body_text="Matnli tushuntirish. " * 40,
                                position=li,
                                is_free=is_free,
                                published_at=utcnow(),
                            )
                            s.add(lesson)
                            await s.flush()
                            quiz_id = None
                            if li % 2 == 0:
                                quiz = Quiz(
                                    lesson_id=lesson.id, title=f"Test {lesson.title}", pass_percent=60
                                )
                                s.add(quiz)
                                await s.flush()
                                quiz_id = quiz.id
                                for qi in range(5):
                                    q = Question(
                                        quiz_id=quiz.id,
                                        type=QuestionType.SINGLE,
                                        text=f"Savol {qi + 1}?",
                                        position=qi,
                                    )
                                    s.add(q)
                                    await s.flush()
                                    correct = rng.randrange(4)
                                    s.add_all(
                                        AnswerOption(
                                            question_id=q.id,
                                            text=f"Variant {oi + 1}",
                                            is_correct=oi == correct,
                                            position=oi,
                                        )
                                        for oi in range(4)
                                    )
                            catalog.lessons.append(
                                LessonInfo(
                                    lesson.id, subject.id, course.id, module.id, topic.id, is_free, quiz_id
                                )
                            )
        await s.flush()

        plan = SubscriptionPlan(name="Oylik", price=Decimal("100000"), duration_days=30)
        s.add(plan)
        await s.flush()

        tg_ids = [TELEGRAM_ID_BASE + i for i in range(users)]
        for start in range(0, users, 2000):
            chunk = tg_ids[start : start + 2000]
            await s.execute(
                insert(User),
                [{"telegram_id": t, "first_name": f"Load{t % 100000}", "language_code": "uz"} for t in chunk],
            )
        await s.execute(insert(UserSettings).from_select(["user_id"], select(User.id)))

        subscribers = rng.sample(tg_ids, int(users * subscriber_share))
        id_by_tg = dict((await s.execute(select(User.telegram_id, User.id))).all())
        now = utcnow()
        for start in range(0, len(subscribers), 2000):
            await s.execute(
                insert(Subscription),
                [
                    {
                        "user_id": id_by_tg[t],
                        "plan_id": plan.id,
                        "starts_at": now - timedelta(days=1),
                        "ends_at": now + timedelta(days=29),
                    }
                    for t in subscribers[start : start + 2000]
                ],
            )
        await s.commit()
    async with engine.connect() as conn:  # outside a transaction block, so stats persist
        await (await conn.execution_options(isolation_level="AUTOCOMMIT")).execute(text("ANALYZE"))
    catalog.subscriber_tg = set(subscribers)
    catalog.all_tg = tg_ids
    return catalog
