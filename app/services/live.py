"""Live sessions (TZ §11). OWNER: COMMERCE.

The join link is secret unless `link_visibility == public`: only `can_see_link()` users get it,
and reminders never include it (students open the session screen, which re-checks access).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AdminPermission,
    Course,
    LiveLinkVisibility,
    LiveSession,
    LiveSessionRegistration,
    LiveSessionStatus,
    Notification,
    NotificationAudience,
    NotificationStatus,
    TeacherAccount,
    User,
)
from app.services import notifications
from app.services.access import has_permission
from app.services.audit import log_action
from app.services.errors import Conflict, InvalidInput, NotFound, SubscriptionRequired
from app.services.subscriptions import has_active_subscription
from app.utils.text import esc
from app.utils.time import fmt_dt, local_tz, utcnow

UPCOMING_GRACE = timedelta(hours=3)  # sessions stay listed this long after their start

STATUS_LABELS = {
    LiveSessionStatus.SCHEDULED: "🗓 Rejalashtirilgan",
    LiveSessionStatus.STARTED: "🔴 Boshlangan",
    LiveSessionStatus.FINISHED: "✅ Yakunlangan",
    LiveSessionStatus.CANCELLED: "🚫 Bekor qilingan",
}
VISIBILITY_LABELS = {
    LiveLinkVisibility.PUBLIC: "Hammaga",
    LiveLinkVisibility.SUBSCRIBERS: "Faqat obunachilarga",
    LiveLinkVisibility.REGISTERED: "Faqat ro‘yxatdan o‘tganlarga",
}
ALLOWED_TRANSITIONS = {
    LiveSessionStatus.SCHEDULED: {LiveSessionStatus.STARTED, LiveSessionStatus.CANCELLED},
    LiveSessionStatus.STARTED: {LiveSessionStatus.FINISHED, LiveSessionStatus.CANCELLED},
    LiveSessionStatus.FINISHED: set(),
    LiveSessionStatus.CANCELLED: set(),
}


def parse_local_datetime(text: str, *, now: datetime | None = None) -> datetime:
    """'25.10.2026 18:30' in the bot timezone → aware UTC datetime in the future."""
    try:
        naive = datetime.strptime(" ".join(text.split()), "%d.%m.%Y %H:%M")
    except ValueError:
        raise InvalidInput(
            "Sanani KK.OO.YYYY SS:DD ko‘rinishida kiriting, masalan: 25.10.2026 18:30"
        ) from None
    value = naive.replace(tzinfo=local_tz())
    if value <= (now or utcnow()):
        raise InvalidInput("Vaqt kelajakda bo‘lishi kerak.")
    return value.astimezone(UTC)


def validate_url(text: str) -> str:
    url = text.strip()
    if not url.startswith(("https://", "http://")) or len(url) > 1000 or any(c.isspace() for c in url):
        raise InvalidInput("Havolani to‘liq kiriting (https://...).")
    return url


# --- access -------------------------------------------------------------------------------


async def is_registered(session: AsyncSession, live_id: int, user_id: int) -> bool:
    return (
        await session.scalar(
            select(LiveSessionRegistration.id).where(
                LiveSessionRegistration.live_session_id == live_id, LiveSessionRegistration.user_id == user_id
            )
        )
        is not None
    )


async def _is_session_teacher(session: AsyncSession, live: LiveSession, user: User) -> bool:
    if live.teacher_id is None:
        return False
    teacher_user_id = await session.scalar(
        select(TeacherAccount.user_id).where(TeacherAccount.id == live.teacher_id)
    )
    if teacher_user_id != user.id:
        return False
    from app.services import teacher_sessions  # AUTH

    return await teacher_sessions.get_active_session(session, user.telegram_id) is not None


async def can_see_link(session: AsyncSession, user: User, live: LiveSession) -> bool:
    if live.join_url is None or live.status in (LiveSessionStatus.FINISHED, LiveSessionStatus.CANCELLED):
        return False
    if has_permission(user, AdminPermission.LIVE) or await _is_session_teacher(session, live, user):
        return True
    match live.link_visibility:
        case LiveLinkVisibility.PUBLIC:
            return True
        case LiveLinkVisibility.SUBSCRIBERS:
            return await has_active_subscription(session, user.id)
        case LiveLinkVisibility.REGISTERED:
            return await is_registered(session, live.id, user.id) and await has_active_subscription(
                session, user.id
            )
    return False


# --- student ------------------------------------------------------------------------------


async def list_upcoming(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 20
) -> list[LiveSession]:
    now = now or utcnow()
    stmt = (
        select(LiveSession)
        .where(
            LiveSession.status.in_([LiveSessionStatus.SCHEDULED, LiveSessionStatus.STARTED]),
            LiveSession.starts_at > now - UPCOMING_GRACE,
        )
        .order_by(LiveSession.starts_at)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def get_live(session: AsyncSession, live_id: int) -> LiveSession:
    live = await session.get(LiveSession, live_id)
    if live is None:
        raise NotFound("Jonli dars topilmadi.")
    return live


async def register(session: AsyncSession, user: User, live_id: int) -> bool:
    """Register for a session. Returns False if already registered."""
    live = await get_live(session, live_id)
    if not live.registration_enabled or live.status != LiveSessionStatus.SCHEDULED:
        raise Conflict("Bu darsga ro‘yxatdan o‘tish yopiq.")
    if live.link_visibility != LiveLinkVisibility.PUBLIC and not await has_active_subscription(
        session, user.id
    ):
        raise SubscriptionRequired("Bu jonli darsda qatnashish uchun faol obuna kerak.")
    result = await session.execute(
        insert(LiveSessionRegistration)
        .values(live_session_id=live.id, user_id=user.id)
        .on_conflict_do_nothing()
        .returning(LiveSessionRegistration.id)
    )
    return result.scalar_one_or_none() is not None


async def unregister(session: AsyncSession, user: User, live_id: int) -> None:
    registration = await session.scalar(
        select(LiveSessionRegistration).where(
            LiveSessionRegistration.live_session_id == live_id, LiveSessionRegistration.user_id == user.id
        )
    )
    if registration is not None:
        await session.delete(registration)


async def registration_count(session: AsyncSession, live_id: int) -> int:
    return (
        await session.scalar(select(func.count()).where(LiveSessionRegistration.live_session_id == live_id))
        or 0
    )


# --- admin --------------------------------------------------------------------------------


@dataclass(slots=True)
class LiveDraft:
    title: str
    starts_at: datetime
    description: str | None = None
    requirements: str | None = None
    duration_minutes: int | None = None
    join_url: str | None = None
    link_visibility: LiveLinkVisibility = LiveLinkVisibility.SUBSCRIBERS
    registration_enabled: bool = False
    course_id: int | None = None


async def create_live(session: AsyncSession, actor: User, draft: LiveDraft) -> LiveSession:
    if draft.course_id is not None and await session.get(Course, draft.course_id) is None:
        raise NotFound("Kurs topilmadi.")
    live = LiveSession(
        title=draft.title,
        description=draft.description,
        requirements=draft.requirements,
        starts_at=draft.starts_at,
        duration_minutes=draft.duration_minutes,
        join_url=draft.join_url,
        link_visibility=draft.link_visibility,
        registration_enabled=draft.registration_enabled,
        course_id=draft.course_id,
        created_by_user_id=actor.id,
    )
    session.add(live)
    await session.flush()
    await log_action(
        session,
        "live.created",
        actor=actor,
        entity_type="live_session",
        entity_id=live.id,
        details={"title": live.title, "starts_at": live.starts_at.isoformat()},
    )
    return live


async def list_admin(
    session: AsyncSession, *, include_past: bool = False, limit: int = 20
) -> list[LiveSession]:
    stmt = select(LiveSession)
    if not include_past:
        stmt = stmt.where(LiveSession.status.in_([LiveSessionStatus.SCHEDULED, LiveSessionStatus.STARTED]))
    stmt = stmt.order_by(LiveSession.starts_at.desc() if include_past else LiveSession.starts_at).limit(limit)
    return list((await session.scalars(stmt)).all())


async def set_status(
    session: AsyncSession, actor: User, live_id: int, status: LiveSessionStatus
) -> LiveSession:
    live = await get_live(session, live_id)
    if status not in ALLOWED_TRANSITIONS[live.status]:
        raise Conflict("Bu holatga o‘tkazib bo‘lmaydi.")
    old = live.status
    live.status = status
    await log_action(
        session,
        "live.status_changed",
        actor=actor,
        entity_type="live_session",
        entity_id=live.id,
        details={"old": old.value, "new": status.value},
    )
    return live


LIVE_FIELDS = ("title", "description", "requirements", "starts_at", "duration_minutes", "join_url")


async def update_field(session: AsyncSession, actor: User, live_id: int, field: str, raw: str) -> LiveSession:
    live = await get_live(session, live_id)
    if live.status in (LiveSessionStatus.FINISHED, LiveSessionStatus.CANCELLED):
        raise Conflict("Yakunlangan darsni tahrirlab bo‘lmaydi.")
    raw = raw.strip()
    clear = raw == "-"
    match field:
        case "title":
            if not raw or len(raw) > 255:
                raise InvalidInput("Nom 1–255 belgi bo‘lishi kerak.")
            live.title = raw
        case "description" | "requirements":
            if len(raw) > 2000:
                raise InvalidInput("Matn 2000 belgidan oshmasligi kerak.")
            setattr(live, field, None if clear else raw)
        case "starts_at":
            live.starts_at = parse_local_datetime(raw)
        case "duration_minutes":
            live.duration_minutes = None if clear else parse_minutes(raw)
        case "join_url":
            live.join_url = None if clear else validate_url(raw)
        case _:
            raise InvalidInput()
    # The link itself is never written to the audit log.
    await log_action(
        session,
        "live.updated",
        actor=actor,
        entity_type="live_session",
        entity_id=live.id,
        details={"field": field},
    )
    return live


def parse_minutes(text: str) -> int:
    try:
        minutes = int(text.strip())
    except ValueError:
        raise InvalidInput("Daqiqalarni butun son bilan kiriting.") from None
    if not 1 <= minutes <= 24 * 60:
        raise InvalidInput("Davomiylik 1–1440 daqiqa bo‘lishi kerak.")
    return minutes


async def set_course(session: AsyncSession, actor: User, live_id: int, course_id: int | None) -> LiveSession:
    live = await get_live(session, live_id)
    if course_id is not None and await session.get(Course, course_id) is None:
        raise NotFound("Kurs topilmadi.")
    live.course_id = course_id
    await log_action(
        session,
        "live.updated",
        actor=actor,
        entity_type="live_session",
        entity_id=live.id,
        details={"field": "course_id", "new": course_id},
    )
    return live


async def toggle_registration(session: AsyncSession, actor: User, live_id: int) -> LiveSession:
    live = await get_live(session, live_id)
    live.registration_enabled = not live.registration_enabled
    await log_action(
        session,
        "live.updated",
        actor=actor,
        entity_type="live_session",
        entity_id=live.id,
        details={"field": "registration_enabled", "new": live.registration_enabled},
    )
    return live


async def cycle_visibility(session: AsyncSession, actor: User, live_id: int) -> LiveSession:
    order = list(LiveLinkVisibility)
    live = await get_live(session, live_id)
    live.link_visibility = order[(order.index(live.link_visibility) + 1) % len(order)]
    await log_action(
        session,
        "live.updated",
        actor=actor,
        entity_type="live_session",
        entity_id=live.id,
        details={"field": "link_visibility", "new": live.link_visibility.value},
    )
    return live


# --- reminders (worker cron) --------------------------------------------------------------


def due_offset(starts_at: datetime, now: datetime, offsets: list[int]) -> int | None:
    """Smallest reminder offset (minutes) whose window we are in, so a late-created session
    gets only the nearest reminder instead of all of them."""
    remaining = starts_at - now
    if remaining <= timedelta(0):
        return None
    eligible = [o for o in offsets if remaining <= timedelta(minutes=o)]
    return min(eligible) if eligible else None


async def queue_due_reminders(
    session: AsyncSession, offsets: list[int], *, now: datetime | None = None
) -> int:
    now = now or utcnow()
    if not offsets:
        return 0
    stmt = select(LiveSession).where(
        LiveSession.status == LiveSessionStatus.SCHEDULED,
        LiveSession.starts_at > now,
        LiveSession.starts_at <= now + timedelta(minutes=max(offsets)),
    )
    created = 0
    for live in (await session.scalars(stmt)).all():
        offset = due_offset(live.starts_at, now, offsets)
        if offset is None:
            continue
        if live.registration_enabled:
            user_ids = list(
                (
                    await session.scalars(
                        select(LiveSessionRegistration.user_id).where(
                            LiveSessionRegistration.live_session_id == live.id
                        )
                    )
                ).all()
            )
            audience_type, audience = NotificationAudience.USERS, {"user_ids": user_ids}
        elif live.course_id is not None:
            audience_type, audience = NotificationAudience.COURSE, {"course_id": live.course_id}
        else:
            audience_type, audience = NotificationAudience.ALL, {}
        stmt_insert = (
            insert(Notification)
            .values(
                kind=notifications.KIND_LIVE_REMINDER,
                title="Jonli dars yaqinlashmoqda",
                body=(
                    f"📅 <b>{esc(live.title)}</b>\n"
                    f"Boshlanish: {fmt_dt(live.starts_at)}\n\n"
                    "Ulanish ma’lumotlari 📅 Jonli darslar bo‘limida."
                ),
                audience_type=audience_type,
                audience={**audience, "live_id": live.id, "starts_at": live.starts_at.isoformat()},
                dedupe_key=f"live:{live.id}:{offset}:{live.starts_at.isoformat()}",
                status=NotificationStatus.QUEUED,
            )
            .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
            .returning(Notification.id)
        )
        notification_id = await session.scalar(stmt_insert)
        if notification_id is not None:
            created += 1
            notifications.enqueue_after_commit(session, notification_id)
    return created
