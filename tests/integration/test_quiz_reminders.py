"""Quiz notifications (TZ §10.1 "test bo‘yicha muhim eslatmalar"). Skipped without TEST_DATABASE_URL."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.database.models import AdminPermission, AttemptStatus, Notification, UserRole
from app.services import lessons, notifications, quizzes
from app.utils.time import utcnow
from tests.integration.content_factory import make_lessons, make_quiz, make_student, make_tree

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_queue(monkeypatch):
    async def noop(notification_id: int) -> None:
        return None

    monkeypatch.setattr(notifications, "enqueue_send", noop)


async def _count(session, kind: str) -> int:
    stmt = select(func.count()).select_from(Notification).where(Notification.kind == kind)
    return await session.scalar(stmt)


async def _started_attempt(session, *, hours_ago: float, **quiz_settings):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    quiz, _ = await make_quiz(session, free[0], **quiz_settings)
    user = await make_student(session)
    await lessons.open_lesson(session, user, free[0])
    attempt = await quizzes.start_or_resume(session, user, quiz.id)
    attempt.started_at = utcnow() - timedelta(hours=hours_ago)
    await session.flush()
    return attempt


async def test_unfinished_attempt_is_reminded_once(session):
    attempt = await _started_attempt(session, hours_ago=7)
    assert await quizzes.queue_unfinished_reminders(session) == 1
    assert await quizzes.queue_unfinished_reminders(session) == 0  # dedupe per attempt

    stmt = select(Notification).where(Notification.kind == notifications.KIND_QUIZ_UNFINISHED)
    n = await session.scalar(stmt)
    recheck = notifications.RECHECKS[notifications.KIND_QUIZ_UNFINISHED]
    assert await recheck(session, n, utcnow())
    attempt.status = AttemptStatus.COMPLETED  # finished before the worker sent it: stale
    await session.flush()
    assert not await recheck(session, n, utcnow())


async def test_recent_or_timed_out_attempts_are_not_reminded(session):
    await _started_attempt(session, hours_ago=1)
    timed = await _started_attempt(session, hours_ago=7, time_limit_seconds=600)
    assert timed.deadline_at is not None
    timed.deadline_at = utcnow() - timedelta(hours=6)
    await session.flush()
    assert await quizzes.queue_unfinished_reminders(session) == 0


async def test_new_quiz_is_announced_once_on_first_activation(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    quiz, _ = await make_quiz(session, free[0])
    quiz.is_active = False
    admin = await make_student(session)
    admin.role, admin.permissions = UserRole.ADMIN, [AdminPermission.QUIZZES.value]
    await session.flush()

    await quizzes.toggle_quiz_flag(session, admin, quiz.id, "is_active")
    assert quiz.is_active and await _count(session, notifications.KIND_NEW_QUIZ) == 1
    await quizzes.toggle_quiz_flag(session, admin, quiz.id, "is_active")  # off
    await quizzes.toggle_quiz_flag(session, admin, quiz.id, "is_active")  # on again
    assert await _count(session, notifications.KIND_NEW_QUIZ) == 1
