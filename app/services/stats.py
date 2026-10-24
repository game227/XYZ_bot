"""Admin statistics (TZ §17). OWNER: COMMERCE.

Each figure is one aggregate query backed by an index; the whole report is cached in Redis
for a minute so repeated taps don't hit the database.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AttemptStatus,
    Course,
    CourseEnrollment,
    DeliveryStatus,
    Lesson,
    LessonAccess,
    Notification,
    NotificationDelivery,
    PaymentRequest,
    PaymentRequestStatus,
    Quiz,
    QuizAttempt,
    Subscription,
    SubscriptionStatus,
    TeacherAccount,
    User,
    UserStatus,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

CACHE_KEY = "stats:report:v1"
CACHE_TTL = 60
FINISHED = [AttemptStatus.COMPLETED, AttemptStatus.EXPIRED, AttemptStatus.INTERRUPTED]


@dataclass(slots=True)
class StatsReport:
    generated_at: str
    users_total: int = 0
    users_active_7d: int = 0
    users_active_30d: int = 0
    users_new_7d: int = 0
    users_blocked_bot: int = 0
    users_blocked: int = 0
    teachers_total: int = 0
    teachers_blocked: int = 0
    subs_active: int = 0
    subs_expired: int = 0
    payments_pending: int = 0
    payments_approved_30d: int = 0
    revenue_30d: str = "0"
    courses_active: int = 0
    lessons_active: int = 0
    lessons_free: int = 0
    lesson_opens_7d: int = 0
    learners_total: int = 0
    avg_lessons_per_learner: str = "0"
    top_courses: list[tuple[str, int]] = field(default_factory=list)
    quizzes_active: int = 0
    attempts_finished: int = 0
    attempts_30d: int = 0
    avg_percent: str = "0"
    pass_rate: str = "0"
    deliveries_7d: dict[str, int] = field(default_factory=dict)
    last_broadcasts: list[tuple[int, str, int, int, int]] = field(default_factory=list)


async def _count(session: AsyncSession, stmt: Select) -> int:
    return await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


async def build_report(session: AsyncSession, *, now: datetime | None = None) -> StatsReport:
    now = now or utcnow()
    week, month = now - timedelta(days=7), now - timedelta(days=30)
    r = StatsReport(generated_at=now.isoformat())
    scalar = session.scalar

    r.users_total = await scalar(select(func.count(User.id))) or 0
    r.users_active_7d = await scalar(select(func.count(User.id)).where(User.last_seen_at >= week)) or 0
    r.users_active_30d = await scalar(select(func.count(User.id)).where(User.last_seen_at >= month)) or 0
    r.users_new_7d = await scalar(select(func.count(User.id)).where(User.created_at >= week)) or 0
    r.users_blocked_bot = (
        await scalar(select(func.count(User.id)).where(User.bot_blocked_at.is_not(None))) or 0
    )
    r.users_blocked = await scalar(select(func.count(User.id)).where(User.status == UserStatus.BLOCKED)) or 0

    r.teachers_total = await scalar(select(func.count(TeacherAccount.id))) or 0
    r.teachers_blocked = (
        await scalar(select(func.count(TeacherAccount.id)).where(TeacherAccount.is_blocked.is_(True))) or 0
    )

    active_sub = Subscription.status == SubscriptionStatus.ACTIVE
    r.subs_active = (
        await scalar(
            select(func.count(distinct(Subscription.user_id))).where(
                active_sub, Subscription.starts_at <= now, Subscription.ends_at > now
            )
        )
        or 0
    )
    # Users who had a subscription but have no coverage now.
    coverage = select(Subscription.user_id).where(active_sub).group_by(Subscription.user_id)
    r.subs_expired = await _count(session, coverage.having(func.max(Subscription.ends_at) <= now))

    r.payments_pending = (
        await scalar(
            select(func.count(PaymentRequest.id)).where(PaymentRequest.status == PaymentRequestStatus.PENDING)
        )
        or 0
    )
    approved = (PaymentRequest.status == PaymentRequestStatus.APPROVED, PaymentRequest.decided_at >= month)
    r.payments_approved_30d = await scalar(select(func.count(PaymentRequest.id)).where(*approved)) or 0
    r.revenue_30d = str(
        await scalar(select(func.coalesce(func.sum(PaymentRequest.amount), 0)).where(*approved)) or 0
    )

    r.courses_active = await scalar(select(func.count(Course.id)).where(Course.is_active.is_(True))) or 0
    r.lessons_active = await scalar(select(func.count(Lesson.id)).where(Lesson.is_active.is_(True))) or 0
    r.lessons_free = (
        await scalar(
            select(func.count(Lesson.id)).where(Lesson.is_active.is_(True), Lesson.is_free.is_(True))
        )
        or 0
    )
    r.lesson_opens_7d = (
        await scalar(select(func.count(LessonAccess.id)).where(LessonAccess.last_opened_at >= week)) or 0
    )
    r.learners_total = await scalar(select(func.count(distinct(LessonAccess.user_id)))) or 0
    opened = await scalar(select(func.count(LessonAccess.id))) or 0
    r.avg_lessons_per_learner = str(round(Decimal(opened) / r.learners_total, 1)) if r.learners_total else "0"
    top = await session.execute(
        select(Course.title, func.count(CourseEnrollment.id).label("n"))
        .join(CourseEnrollment, CourseEnrollment.course_id == Course.id)
        .group_by(Course.id)
        .order_by(func.count(CourseEnrollment.id).desc())
        .limit(5)
    )
    r.top_courses = [(title, n) for title, n in top.all()]

    r.quizzes_active = await scalar(select(func.count(Quiz.id)).where(Quiz.is_active.is_(True))) or 0
    finished = QuizAttempt.status.in_(FINISHED)
    r.attempts_finished = await scalar(select(func.count(QuizAttempt.id)).where(finished)) or 0
    r.attempts_30d = (
        await scalar(select(func.count(QuizAttempt.id)).where(finished, QuizAttempt.finished_at >= month))
        or 0
    )
    avg, passed = (
        await session.execute(
            select(
                func.avg(QuizAttempt.percent),
                func.avg(case((QuizAttempt.passed.is_(True), 100), else_=0)),
            ).where(finished)
        )
    ).one()
    r.avg_percent = str(round(Decimal(avg), 1)) if avg is not None else "0"
    r.pass_rate = str(round(Decimal(passed), 1)) if passed is not None else "0"

    deliveries = await session.execute(
        select(NotificationDelivery.status, func.count())
        .where(NotificationDelivery.created_at >= week)
        .group_by(NotificationDelivery.status)
    )
    r.deliveries_7d = {str(DeliveryStatus(s).value): n for s, n in deliveries.all()}
    last = await session.execute(
        select(
            Notification.id,
            Notification.status,
            Notification.total_count,
            Notification.sent_count,
            Notification.failed_count,
        )
        .where(Notification.kind == "manual", Notification.created_at >= month)
        .order_by(Notification.created_at.desc())
        .limit(5)
    )
    r.last_broadcasts = [(i, str(s.value), t, sent, failed) for i, s, t, sent, failed in last.all()]
    return r


async def get_report(session: AsyncSession, *, fresh: bool = False) -> StatsReport:
    from app.services.auth import get_redis

    redis = get_redis()
    if not fresh:
        try:
            cached = await redis.get(CACHE_KEY)
            if cached:
                data = json.loads(cached)
                data["top_courses"] = [tuple(x) for x in data["top_courses"]]
                data["last_broadcasts"] = [tuple(x) for x in data["last_broadcasts"]]
                return StatsReport(**data)
        except Exception:  # noqa: BLE001 - cache is optional
            logger.debug("stats cache unavailable", exc_info=True)
    report = await build_report(session)
    try:
        await redis.set(CACHE_KEY, json.dumps(asdict(report)), ex=CACHE_TTL)
    except Exception:  # noqa: BLE001
        logger.debug("stats cache unavailable", exc_info=True)
    return report
