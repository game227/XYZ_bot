"""Free-lesson limit and lesson access against PostgreSQL (TZ §5, acceptance criteria 11–14, 18).

Skipped without TEST_DATABASE_URL.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.database.models import AdminPermission, LessonAccess, User, UserRole
from app.services import lessons
from app.services.errors import AccessDenied, NotFound, SubscriptionRequired
from app.utils.time import utcnow
from tests.integration.content_factory import make_lessons, make_student, make_tree, subscribe

pytestmark = pytest.mark.integration


async def test_two_free_lessons_then_limit(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=3)
    user = await make_student(session)

    assert await lessons.free_lessons_left(session, user) == 2
    first = await lessons.open_lesson(session, user, free[0])
    assert (first.via, first.consumed_free_slot, first.free_left) == ("free", True, 1)
    second = await lessons.open_lesson(session, user, free[1])
    assert (second.consumed_free_slot, second.free_left) == (True, 0)

    with pytest.raises(SubscriptionRequired):
        await lessons.open_lesson(session, user, free[2])
    assert await lessons.free_lessons_left(session, user) == 0
    assert not await lessons.can_view_lesson(session, user, free[2])


async def test_reopen_does_not_consume(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=2)
    user = await make_student(session)

    await lessons.open_lesson(session, user, free[0])
    for _ in range(3):
        again = await lessons.open_lesson(session, user, free[0])
        assert (again.via, again.consumed_free_slot, again.free_left) == ("reopen", False, 1)
    assert await lessons.free_lessons_left(session, user) == 1
    row = await session.scalar(select(LessonAccess).where(LessonAccess.lesson_id == free[0].id))
    assert row is not None and row.open_count == 4 and row.consumed_free_slot
    assert await lessons.can_view_lesson(session, user, free[0])


async def test_paid_lesson_requires_subscription(session):
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    user = await make_student(session)

    with pytest.raises(SubscriptionRequired):
        await lessons.open_lesson(session, user, paid[0])
    assert not await lessons.can_view_lesson(session, user, paid[0])

    await subscribe(session, user)
    result = await lessons.open_lesson(session, user, paid[0])
    assert (result.via, result.consumed_free_slot) == ("subscription", False)
    assert await lessons.can_view_lesson(session, user, paid[0])


async def test_subscriber_does_not_spend_free_slots(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=3)
    user = await make_student(session)
    await subscribe(session, user)
    for lesson in free:
        assert (await lessons.open_lesson(session, user, lesson)).consumed_free_slot is False
    assert await lessons.free_lessons_left(session, user) == 2


async def test_expired_subscription_closes_paid_lessons_keeps_free(session):
    topic = await make_tree(session)
    free, paid = await make_lessons(session, topic, free=1, paid=1)
    user = await make_student(session)
    await lessons.open_lesson(session, user, free[0])
    sub = await subscribe(session, user)
    await lessons.open_lesson(session, user, paid[0])

    sub.ends_at = utcnow() - timedelta(seconds=1)
    await session.flush()
    assert not await lessons.can_view_lesson(session, user, paid[0])
    with pytest.raises(SubscriptionRequired):
        await lessons.open_lesson(session, user, paid[0])
    assert await lessons.can_view_lesson(session, user, free[0])


async def test_inactive_lesson_hidden_from_students(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    user = await make_student(session)
    topic.is_active = False  # a hidden parent hides the lesson too
    await session.flush()
    with pytest.raises(NotFound):
        await lessons.open_lesson(session, user, free[0])
    assert await lessons.free_lessons_left(session, user) == 2


async def test_content_admin_sees_everything_without_consuming(session, make_user):
    topic = await make_tree(session)
    free, paid = await make_lessons(session, topic, free=1, paid=1)
    admin = await make_user(role=UserRole.ADMIN, permissions=[AdminPermission.CONTENT.value])
    free[0].is_active = False
    await session.flush()
    for lesson in (*free, *paid):
        assert await lessons.can_view_lesson(session, admin, lesson)
        assert (await lessons.open_lesson(session, admin, lesson)).via == "staff"
    assert await lessons.free_lessons_left(session, admin) == 2


async def test_student_cannot_edit_content(session):
    topic = await make_tree(session)
    user = await make_student(session)
    with pytest.raises(AccessDenied):
        await lessons.create_lesson(session, user, topic_id=topic.id, title="X")
    with pytest.raises(AccessDenied):
        await lessons.create_subject(session, user, title="X")


async def test_delete_refuses_used_lesson(session, make_user):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    admin = await make_user(role=UserRole.ADMIN, permissions=[AdminPermission.CONTENT.value])
    user = await make_student(session)
    await lessons.open_lesson(session, user, free[0])
    from app.services.errors import Conflict

    with pytest.raises(Conflict):
        await lessons.delete_node(session, admin, "lesson", free[0].id)
    with pytest.raises(Conflict):
        await lessons.delete_node(session, admin, "topic", topic.id)


# --------------------------------------------------------------------------- concurrency


async def _setup_committed(sessionmaker, *, free: int):
    async with sessionmaker() as s:
        topic = await make_tree(s)
        free_lessons, _ = await make_lessons(s, topic, free=free)
        user = await make_student(s)
        await s.commit()
        return user.id, [lsn.id for lsn in free_lessons]


async def _open_in_own_session(sessionmaker, user_id: int, lesson_id: int) -> str:
    async with sessionmaker() as s:
        user = await s.get(User, user_id)
        lesson = await lessons.get_lesson(s, lesson_id)
        try:
            result = await lessons.open_lesson(s, user, lesson)
        except SubscriptionRequired:
            await s.rollback()
            return "denied"
        await s.commit()
        return result.via


async def _consumed(sessionmaker, user_id: int) -> int:
    async with sessionmaker() as s:
        stmt = select(func.count()).where(LessonAccess.user_id == user_id, LessonAccess.consumed_free_slot)
        return await s.scalar(stmt) or 0


async def test_parallel_opens_never_exceed_limit(committing_sessionmaker):
    user_id, lesson_ids = await _setup_committed(committing_sessionmaker, free=5)
    results = await asyncio.gather(
        *(_open_in_own_session(committing_sessionmaker, user_id, lid) for lid in lesson_ids)
    )
    assert sorted(results) == ["denied", "denied", "denied", "free", "free"]
    assert await _consumed(committing_sessionmaker, user_id) == 2


async def test_parallel_opens_of_same_lesson_consume_once(committing_sessionmaker):
    user_id, lesson_ids = await _setup_committed(committing_sessionmaker, free=1)
    results = await asyncio.gather(
        *(_open_in_own_session(committing_sessionmaker, user_id, lesson_ids[0]) for _ in range(6))
    )
    assert sorted(results) == ["free"] + ["reopen"] * 5
    assert await _consumed(committing_sessionmaker, user_id) == 1
