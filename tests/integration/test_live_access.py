"""Live-session link visibility against PostgreSQL."""

from datetime import timedelta

import pytest

from app.database.models import LiveLinkVisibility, UserRole
from app.services import live, notifications, subscriptions
from app.services.errors import SubscriptionRequired
from app.utils.time import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_queue(monkeypatch):
    async def noop(_: int) -> None:
        return None

    monkeypatch.setattr(notifications, "enqueue_send", noop)


async def _session(session, admin, visibility, registration=False):
    return await live.create_live(
        session,
        admin,
        live.LiveDraft(
            title="Jonli dars",
            starts_at=utcnow() + timedelta(days=1),
            join_url="https://meet.example/secret",
            link_visibility=visibility,
            registration_enabled=registration,
        ),
    )


async def test_link_visibility(session, make_user):
    admin = await make_user(role=UserRole.ADMIN, permissions=["live"])
    student = await make_user()
    public = await _session(session, admin, LiveLinkVisibility.PUBLIC)
    subs_only = await _session(session, admin, LiveLinkVisibility.SUBSCRIBERS)
    registered = await _session(session, admin, LiveLinkVisibility.REGISTERED, registration=True)

    assert await live.can_see_link(session, student, public)
    assert not await live.can_see_link(session, student, subs_only)
    assert not await live.can_see_link(session, student, registered)
    with pytest.raises(SubscriptionRequired):
        await live.register(session, student, registered.id)

    await subscriptions.grant_subscription(session, admin, student.id, 30)
    assert await live.can_see_link(session, student, subs_only)
    assert not await live.can_see_link(session, student, registered)  # not registered yet
    assert await live.register(session, student, registered.id)
    assert not await live.register(session, student, registered.id)  # idempotent
    assert await live.can_see_link(session, student, registered)
    assert await live.can_see_link(session, admin, subs_only)  # staff always


async def test_live_reminder_once_per_offset(session, make_user):
    admin = await make_user(role=UserRole.ADMIN, permissions=["live"])
    item = await _session(session, admin, LiveLinkVisibility.PUBLIC)
    item.starts_at = utcnow() + timedelta(minutes=30)
    await session.flush()
    assert await live.queue_due_reminders(session, [1440, 60]) == 1
    assert await live.queue_due_reminders(session, [1440, 60]) == 0
