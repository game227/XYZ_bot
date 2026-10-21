"""Educational content: Subject → Course → Module → Topic → Lesson, materials, free-lesson limit.

OWNER: CONTENT.

Access rules (TZ §4, §5):
- Admins with the CONTENT permission and teachers with an active session assigned to the
  course see everything of that course (inactive items included).
- Students see only active items whose whole parent chain is active.
- Paid lessons need an active subscription. A non-subscriber may open at most
  `free_lesson_limit` distinct free lessons in the whole bot; reopening never re-consumes.

Concurrency of the free limit: `open_lesson` locks the user's `users` row
(`SELECT ... FOR UPDATE`), counts consumed slots and inserts the access row in the same
transaction. Callers must commit right after `open_lesson` (before slow Telegram calls).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Select, and_, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import (
    AdminPermission,
    Course,
    CourseEnrollment,
    Lesson,
    LessonAccess,
    LessonMedia,
    MediaFile,
    MediaKind,
    MediaStatus,
    Module,
    Quiz,
    QuizAttempt,
    Subject,
    Topic,
    User,
    UserRole,
)
from app.services import audit
from app.services.access import has_permission
from app.services.errors import (
    AccessDenied,
    Conflict,
    InvalidInput,
    NotFound,
    SubscriptionRequired,
    TeacherSessionRequired,
)
from app.services.subscriptions import has_active_subscription
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.database.base import Base

logger = logging.getLogger(__name__)

TITLE_MAX = 255
SHORT_DESCRIPTION_MAX = 1000
BODY_MAX = 20_000
CAPTION_MAX = 1000

# Course-assignment rights (TeacherCourseAssignment flags, see AUTH's teacher_course_ids).
RIGHT_CONTENT = "can_manage_content"
RIGHT_QUIZZES = "can_manage_quizzes"

LIMIT_REACHED_MESSAGE = (
    "Siz {limit} ta bepul darsdan foydalanib bo‘ldingiz. "
    "Boshqa darslarni ochish uchun 💳 Obuna olish bo‘limiga o‘ting."
)
PAID_LESSON_MESSAGE = "Bu dars pullik. Uni ochish uchun faol obuna kerak — 💳 Obuna olish bo‘limiga o‘ting."


# --------------------------------------------------------------------------- permissions


async def _teacher_course_ids(session: AsyncSession, user: User, right: str | None) -> set[int]:
    """Course ids of the teacher's active session (empty set if no active session).

    AUTH owns `teacher_sessions`; imported lazily so this module works before it lands.
    """
    if user.role != UserRole.TEACHER:
        return set()
    try:
        from app.services import teacher_sessions
    except ImportError:  # pragma: no cover - AUTH module not merged yet
        return set()
    try:
        ctx = await teacher_sessions.require_teacher(session, user)
    except TeacherSessionRequired:
        return set()
    return set(await teacher_sessions.teacher_course_ids(session, ctx.teacher.id, right=right))


async def manageable_course_ids(
    session: AsyncSession, user: User, *, quizzes: bool = False
) -> set[int] | None:
    """Courses `user` may manage. None means "all" (admin with the permission)."""
    perm = AdminPermission.QUIZZES if quizzes else AdminPermission.CONTENT
    if has_permission(user, perm):
        return None
    return await _teacher_course_ids(session, user, RIGHT_QUIZZES if quizzes else RIGHT_CONTENT)


async def ensure_can_manage_course(
    session: AsyncSession, user: User, course_id: int, *, quizzes: bool = False
) -> None:
    """Raise unless `user` may edit content (or quizzes) of the course.

    Teachers need an active 24h session on every call (TeacherSessionRequired otherwise).
    """
    perm = AdminPermission.QUIZZES if quizzes else AdminPermission.CONTENT
    if has_permission(user, perm):
        return
    if user.role == UserRole.TEACHER:
        try:
            from app.services import teacher_sessions
        except ImportError:  # pragma: no cover
            raise TeacherSessionRequired() from None
        ctx = await teacher_sessions.require_teacher(session, user)
        right = RIGHT_QUIZZES if quizzes else RIGHT_CONTENT
        if course_id in await teacher_sessions.teacher_course_ids(session, ctx.teacher.id, right=right):
            return
    raise AccessDenied()


def ensure_content_admin(user: User) -> None:
    """Subjects and courses themselves are managed by admins only."""
    if not has_permission(user, AdminPermission.CONTENT):
        raise AccessDenied()


async def can_preview_course(session: AsyncSession, user: User, course_id: int) -> bool:
    """Staff preview: content admins, or teachers with an active session on the course (any right)."""
    if has_permission(user, AdminPermission.CONTENT) or has_permission(user, AdminPermission.QUIZZES):
        return True
    return course_id in await _teacher_course_ids(session, user, None)


# --------------------------------------------------------------------------- lookups


async def get_subject(session: AsyncSession, subject_id: int) -> Subject:
    obj = await session.get(Subject, subject_id)
    if obj is None:
        raise NotFound()
    return obj


async def get_course(session: AsyncSession, course_id: int) -> Course:
    obj = await session.get(Course, course_id)
    if obj is None:
        raise NotFound()
    return obj


async def get_module(session: AsyncSession, module_id: int) -> Module:
    obj = await session.get(Module, module_id)
    if obj is None:
        raise NotFound()
    return obj


async def get_topic(session: AsyncSession, topic_id: int) -> Topic:
    obj = await session.get(Topic, topic_id)
    if obj is None:
        raise NotFound()
    return obj


async def get_lesson(session: AsyncSession, lesson_id: int) -> Lesson:
    obj = await session.get(Lesson, lesson_id)
    if obj is None:
        raise NotFound()
    return obj


def _public_lessons() -> Select[tuple[Lesson]]:
    """Lessons visible to students: the lesson and its whole parent chain are active."""
    return (
        select(Lesson)
        .join(Topic, Topic.id == Lesson.topic_id)
        .join(Module, Module.id == Topic.module_id)
        .join(Course, Course.id == Lesson.course_id)
        .join(Subject, Subject.id == Course.subject_id)
        .where(
            Lesson.is_active.is_(True),
            Topic.is_active.is_(True),
            Module.is_active.is_(True),
            Course.is_active.is_(True),
            Subject.is_active.is_(True),
        )
    )


async def is_lesson_public(session: AsyncSession, lesson: Lesson) -> bool:
    stmt = _public_lessons().where(Lesson.id == lesson.id).with_only_columns(Lesson.id)
    return await session.scalar(stmt) is not None


@dataclass(slots=True)
class LessonPath:
    subject: Subject
    course: Course
    module: Module
    topic: Topic


async def lesson_path(session: AsyncSession, lesson: Lesson) -> LessonPath:
    topic = await get_topic(session, lesson.topic_id)
    module = await get_module(session, topic.module_id)
    course = await get_course(session, module.course_id)
    subject = await get_subject(session, course.subject_id)
    return LessonPath(subject=subject, course=course, module=module, topic=topic)


# --------------------------------------------------------------------------- student listings


async def list_subjects(session: AsyncSession, *, include_inactive: bool = False) -> list[Subject]:
    stmt = select(Subject).order_by(Subject.position, Subject.id)
    if not include_inactive:
        stmt = stmt.where(Subject.is_active.is_(True))
    return list(await session.scalars(stmt))


async def list_courses(
    session: AsyncSession,
    subject_id: int | None = None,
    *,
    include_inactive: bool = False,
    course_ids: set[int] | None = None,
) -> list[Course]:
    stmt = select(Course).order_by(Course.position, Course.id)
    if subject_id is not None:
        stmt = stmt.where(Course.subject_id == subject_id)
    if course_ids is not None:
        stmt = stmt.where(Course.id.in_(course_ids or {-1}))
    if not include_inactive:
        stmt = stmt.join(Subject, Subject.id == Course.subject_id).where(
            Course.is_active.is_(True), Subject.is_active.is_(True)
        )
    return list(await session.scalars(stmt))


async def list_modules(
    session: AsyncSession, course_id: int, *, include_inactive: bool = False
) -> list[Module]:
    stmt = select(Module).where(Module.course_id == course_id).order_by(Module.position, Module.id)
    if not include_inactive:
        stmt = stmt.where(Module.is_active.is_(True))
    return list(await session.scalars(stmt))


async def list_topics(
    session: AsyncSession, module_id: int, *, include_inactive: bool = False
) -> list[Topic]:
    stmt = select(Topic).where(Topic.module_id == module_id).order_by(Topic.position, Topic.id)
    if not include_inactive:
        stmt = stmt.where(Topic.is_active.is_(True))
    return list(await session.scalars(stmt))


async def list_lessons(
    session: AsyncSession, topic_id: int, *, include_inactive: bool = False
) -> list[Lesson]:
    stmt = select(Lesson).where(Lesson.topic_id == topic_id).order_by(Lesson.position, Lesson.id)
    if not include_inactive:
        stmt = stmt.where(Lesson.is_active.is_(True))
    return list(await session.scalars(stmt))


async def list_free_lessons(
    session: AsyncSession, *, limit: int = 10, offset: int = 0
) -> tuple[list[Lesson], int]:
    base = _public_lessons().where(Lesson.is_free.is_(True))
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(Course.position, Course.id, Lesson.position, Lesson.id).limit(limit).offset(offset)
    return list(await session.scalars(stmt)), total


def _has_ready_video():
    return exists().where(
        LessonMedia.lesson_id == Lesson.id,
        MediaFile.id == LessonMedia.media_id,
        MediaFile.kind == MediaKind.VIDEO,
        MediaFile.status == MediaStatus.READY,
    )


async def list_video_lessons(
    session: AsyncSession, *, limit: int = 10, offset: int = 0
) -> tuple[list[Lesson], int]:
    base = _public_lessons().where(_has_ready_video())
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(Course.position, Course.id, Lesson.position, Lesson.id).limit(limit).offset(offset)
    return list(await session.scalars(stmt)), total


async def lesson_materials(
    session: AsyncSession, lesson_id: int, *, ready_only: bool = True
) -> list[tuple[LessonMedia, MediaFile]]:
    stmt = (
        select(LessonMedia, MediaFile)
        .join(MediaFile, MediaFile.id == LessonMedia.media_id)
        .where(LessonMedia.lesson_id == lesson_id)
        .order_by(LessonMedia.position, LessonMedia.id)
    )
    if ready_only:
        stmt = stmt.where(MediaFile.status == MediaStatus.READY)
    return [(lm, mf) for lm, mf in (await session.execute(stmt)).all()]


async def get_lesson_material(session: AsyncSession, lesson_id: int, media_id: int) -> MediaFile:
    stmt = (
        select(MediaFile)
        .join(LessonMedia, LessonMedia.media_id == MediaFile.id)
        .where(LessonMedia.lesson_id == lesson_id, MediaFile.id == media_id)
    )
    media = await session.scalar(stmt)
    if media is None:
        raise NotFound()
    return media


async def opened_free_lesson_ids(session: AsyncSession, user_id: int) -> set[int]:
    """Lessons opened through the free allowance (they stay open for this user)."""
    stmt = select(LessonAccess.lesson_id).where(
        LessonAccess.user_id == user_id, LessonAccess.consumed_free_slot.is_(True)
    )
    return set(await session.scalars(stmt))


# --------------------------------------------------------------------------- access + free limit


async def free_lessons_used(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.count()).where(
        LessonAccess.user_id == user_id, LessonAccess.consumed_free_slot.is_(True)
    )
    return await session.scalar(stmt) or 0


async def free_lessons_left(session: AsyncSession, user: User) -> int:
    return max(0, get_settings().free_lesson_limit - await free_lessons_used(session, user.id))


async def _consumed_access(session: AsyncSession, user_id: int, lesson_id: int) -> bool:
    stmt = select(LessonAccess.consumed_free_slot).where(
        LessonAccess.user_id == user_id, LessonAccess.lesson_id == lesson_id
    )
    return bool(await session.scalar(stmt))


async def can_view_lesson(session: AsyncSession, user: User, lesson: Lesson) -> bool:
    """Whether `user` may see the lesson's content right now. No side effects.

    True for content staff (admin perm / teacher with an active session on the course),
    active subscribers (public lessons), and users who already opened this free lesson
    through their allowance. A free lesson not yet opened returns False: it has to be
    opened (and a slot consumed) with `open_lesson` first.
    """
    if await can_preview_course(session, user, lesson.course_id):
        return True
    if not await is_lesson_public(session, lesson):
        return False
    if await has_active_subscription(session, user.id):
        return True
    return lesson.is_free and await _consumed_access(session, user.id, lesson.id)


@dataclass(slots=True)
class LessonOpenResult:
    lesson: Lesson
    via: str  # "staff" | "subscription" | "free" | "reopen"
    consumed_free_slot: bool  # True when this call consumed a new free slot
    free_left: int | None  # remaining free slots for non-subscribers, else None


async def _record_access(session: AsyncSession, user_id: int, lesson: Lesson, *, consumed: bool) -> None:
    now = utcnow()
    stmt = insert(LessonAccess).values(
        user_id=user_id,
        lesson_id=lesson.id,
        consumed_free_slot=consumed,
        first_opened_at=now,
        last_opened_at=now,
        open_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[LessonAccess.user_id, LessonAccess.lesson_id],
        set_={
            "last_opened_at": now,
            "open_count": LessonAccess.open_count + 1,
            # Once consumed, a slot stays consumed (the lesson stays open for the user).
            "consumed_free_slot": LessonAccess.consumed_free_slot | stmt.excluded.consumed_free_slot,
        },
    )
    await session.execute(stmt)
    await session.execute(
        insert(CourseEnrollment)
        .values(user_id=user_id, course_id=lesson.course_id)
        .on_conflict_do_nothing(index_elements=[CourseEnrollment.user_id, CourseEnrollment.course_id])
    )


async def open_lesson(session: AsyncSession, user: User, lesson: Lesson) -> LessonOpenResult:
    """Open a lesson for `user`: check access, record the open, consume a free slot if needed.

    Raises NotFound (hidden lesson) or SubscriptionRequired (paid lesson / limit used up).
    The caller must commit right away to release the users-row lock.
    """
    if await can_preview_course(session, user, lesson.course_id):
        await _record_access(session, user.id, lesson, consumed=False)
        return LessonOpenResult(lesson, "staff", False, None)

    if not await is_lesson_public(session, lesson):
        raise NotFound("Dars topilmadi yoki hozircha mavjud emas.")

    if await has_active_subscription(session, user.id):
        await _record_access(session, user.id, lesson, consumed=False)
        return LessonOpenResult(lesson, "subscription", False, None)

    if not lesson.is_free:
        raise SubscriptionRequired(PAID_LESSON_MESSAGE)

    limit = get_settings().free_lesson_limit
    # Serialize all free-slot decisions of this user (parallel taps / requests).
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())

    if await _consumed_access(session, user.id, lesson.id):
        await _record_access(session, user.id, lesson, consumed=False)
        used = await free_lessons_used(session, user.id)
        return LessonOpenResult(lesson, "reopen", False, max(0, limit - used))

    used = await free_lessons_used(session, user.id)
    if used >= limit:
        raise SubscriptionRequired(LIMIT_REACHED_MESSAGE.format(limit=limit))

    await _record_access(session, user.id, lesson, consumed=True)
    logger.info("free lesson slot consumed user=%s lesson=%s (%s/%s)", user.id, lesson.id, used + 1, limit)
    return LessonOpenResult(lesson, "free", True, max(0, limit - used - 1))


# --------------------------------------------------------------------------- CRUD helpers


def clean_title(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        raise InvalidInput("Nom bo‘sh bo‘lmasligi kerak.")
    if len(value) > TITLE_MAX:
        raise InvalidInput(f"Nom {TITLE_MAX} belgidan oshmasligi kerak.")
    return value


def clean_text(value: str | None, limit: int) -> str | None:
    value = (value or "").strip()
    if value in {"", "-"}:
        return None
    if len(value) > limit:
        raise InvalidInput(f"Matn {limit} belgidan oshmasligi kerak.")
    return value


async def _next_position(session: AsyncSession, model: type[Base], parent_col, parent_id: int | None) -> int:
    stmt = select(func.coalesce(func.max(model.position), 0))  # type: ignore[attr-defined]
    if parent_col is not None:
        stmt = stmt.where(parent_col == parent_id)
    return (await session.scalar(stmt) or 0) + 10


async def _log(session: AsyncSession, action: str, actor: User, obj: Base, **details: object) -> None:
    await audit.log_action(
        session,
        action,
        actor=actor,
        entity_type=obj.__tablename__,  # type: ignore[attr-defined]
        entity_id=obj.id,  # type: ignore[attr-defined]
        details=details or None,
    )


async def create_subject(
    session: AsyncSession, actor: User, *, title: str, description: str | None = None
) -> Subject:
    ensure_content_admin(actor)
    obj = Subject(
        title=clean_title(title),
        description=clean_text(description, BODY_MAX),
        position=await _next_position(session, Subject, None, None),
    )
    session.add(obj)
    await session.flush()
    await _log(session, "subject.created", actor, obj, title=obj.title)
    return obj


async def create_course(
    session: AsyncSession, actor: User, *, subject_id: int, title: str, description: str | None = None
) -> Course:
    ensure_content_admin(actor)
    await get_subject(session, subject_id)
    obj = Course(
        subject_id=subject_id,
        title=clean_title(title),
        description=clean_text(description, BODY_MAX),
        position=await _next_position(session, Course, Course.subject_id, subject_id),
    )
    session.add(obj)
    await session.flush()
    await _log(session, "course.created", actor, obj, title=obj.title, subject_id=subject_id)
    return obj


async def create_module(
    session: AsyncSession, actor: User, *, course_id: int, title: str, description: str | None = None
) -> Module:
    await get_course(session, course_id)
    await ensure_can_manage_course(session, actor, course_id)
    obj = Module(
        course_id=course_id,
        title=clean_title(title),
        description=clean_text(description, BODY_MAX),
        position=await _next_position(session, Module, Module.course_id, course_id),
    )
    session.add(obj)
    await session.flush()
    await _log(session, "module.created", actor, obj, title=obj.title, course_id=course_id)
    return obj


async def create_topic(
    session: AsyncSession, actor: User, *, module_id: int, title: str, description: str | None = None
) -> Topic:
    module = await get_module(session, module_id)
    await ensure_can_manage_course(session, actor, module.course_id)
    obj = Topic(
        module_id=module_id,
        title=clean_title(title),
        description=clean_text(description, BODY_MAX),
        position=await _next_position(session, Topic, Topic.module_id, module_id),
    )
    session.add(obj)
    await session.flush()
    await _log(session, "topic.created", actor, obj, title=obj.title, module_id=module_id)
    return obj


async def create_lesson(
    session: AsyncSession,
    actor: User,
    *,
    topic_id: int,
    title: str,
    short_description: str | None = None,
    body_text: str | None = None,
    is_free: bool = False,
) -> Lesson:
    topic = await get_topic(session, topic_id)
    module = await get_module(session, topic.module_id)
    await ensure_can_manage_course(session, actor, module.course_id)
    obj = Lesson(
        topic_id=topic_id,
        course_id=module.course_id,
        title=clean_title(title),
        short_description=clean_text(short_description, SHORT_DESCRIPTION_MAX),
        body_text=clean_text(body_text, BODY_MAX),
        is_free=is_free and has_permission(actor, AdminPermission.CONTENT),
        # New lessons start hidden until published, so half-done content never leaks.
        is_active=False,
        position=await _next_position(session, Lesson, Lesson.topic_id, topic_id),
        created_by_user_id=actor.id,
    )
    session.add(obj)
    await session.flush()
    await _log(session, "lesson.created", actor, obj, title=obj.title, topic_id=topic_id)
    return obj


# Editable text fields and their length limits.
_FIELD_LIMITS = {
    "title": TITLE_MAX,
    "description": BODY_MAX,
    "short_description": SHORT_DESCRIPTION_MAX,
    "body_text": BODY_MAX,
}

_MODELS: dict[str, type[Base]] = {
    "subject": Subject,
    "course": Course,
    "module": Module,
    "topic": Topic,
    "lesson": Lesson,
}

_PARENT_ATTR: dict[str, str | None] = {
    "subject": None,
    "course": "subject_id",
    "module": "course_id",
    "topic": "module_id",
    "lesson": "topic_id",
}

_LESSON_FIELDS = {"title", "short_description", "body_text"}
_OTHER_FIELDS = {"title", "description"}


async def course_id_of(session: AsyncSession, kind: str, obj: Base) -> int | None:
    """Course an object belongs to (None for subjects)."""
    match kind:
        case "subject":
            return None
        case "course":
            return obj.id  # type: ignore[attr-defined]
        case "module" | "lesson":
            return obj.course_id  # type: ignore[attr-defined]
        case "topic":
            return (await get_module(session, obj.module_id)).course_id  # type: ignore[attr-defined]
    raise InvalidInput()


async def get_node(session: AsyncSession, kind: str, obj_id: int) -> Base:
    model = _MODELS.get(kind)
    if model is None:
        raise InvalidInput()
    obj = await session.get(model, obj_id)
    if obj is None:
        raise NotFound()
    return obj


async def ensure_can_edit_node(session: AsyncSession, actor: User, kind: str, obj: Base) -> None:
    if kind in {"subject", "course"}:
        ensure_content_admin(actor)
        return
    course_id = await course_id_of(session, kind, obj)
    assert course_id is not None
    await ensure_can_manage_course(session, actor, course_id)


async def update_field(
    session: AsyncSession, actor: User, kind: str, obj_id: int, field: str, value: str
) -> Base:
    allowed = _LESSON_FIELDS if kind == "lesson" else _OTHER_FIELDS
    if field not in allowed:
        raise InvalidInput()
    obj = await get_node(session, kind, obj_id)
    await ensure_can_edit_node(session, actor, kind, obj)
    cleaned = clean_title(value) if field == "title" else clean_text(value, _FIELD_LIMITS[field])
    setattr(obj, field, cleaned)
    await session.flush()
    await _log(session, f"{kind}.updated", actor, obj, field=field)
    return obj


async def toggle_active(session: AsyncSession, actor: User, kind: str, obj_id: int) -> Base:
    obj = await get_node(session, kind, obj_id)
    await ensure_can_edit_node(session, actor, kind, obj)
    if kind == "lesson" and not obj.is_active and obj.published_at is None:  # type: ignore[attr-defined]
        # First activation of a lesson is a publication: it must be announced.
        return await publish_lesson(session, actor, obj_id)
    obj.is_active = not obj.is_active  # type: ignore[attr-defined]
    await session.flush()
    await _log(session, f"{kind}.updated", actor, obj, is_active=obj.is_active)  # type: ignore[attr-defined]
    return obj


async def set_lesson_free(session: AsyncSession, actor: User, lesson_id: int, is_free: bool) -> Lesson:
    """Only admins decide which lessons are free (TZ §5.1.3)."""
    ensure_content_admin(actor)
    lesson = await get_lesson(session, lesson_id)
    lesson.is_free = is_free
    await session.flush()
    await _log(session, "lesson.updated", actor, lesson, is_free=is_free)
    return lesson


async def move(session: AsyncSession, actor: User, kind: str, obj_id: int, direction: int) -> Base:
    """Swap position with the previous (-1) or next (+1) sibling."""
    obj = await get_node(session, kind, obj_id)
    await ensure_can_edit_node(session, actor, kind, obj)
    model = type(obj)
    parent_attr = _PARENT_ATTR[kind]
    stmt = select(model)
    if parent_attr:
        stmt = stmt.where(getattr(model, parent_attr) == getattr(obj, parent_attr))
    siblings = list(await session.scalars(stmt.order_by(model.position, model.id)))  # type: ignore[attr-defined]
    idx = next(i for i, s in enumerate(siblings) if s.id == obj.id)  # type: ignore[attr-defined]
    other = idx + (1 if direction > 0 else -1)
    if 0 <= other < len(siblings):
        siblings[idx], siblings[other] = siblings[other], siblings[idx]
        for pos, sibling in enumerate(siblings, start=1):
            sibling.position = pos * 10  # type: ignore[attr-defined]
        await session.flush()
    return obj


async def _has_children(session: AsyncSession, kind: str, obj_id: int) -> bool:
    child_checks = {
        "subject": select(Course.id).where(Course.subject_id == obj_id),
        "course": select(Module.id).where(Module.course_id == obj_id),
        "module": select(Topic.id).where(Topic.module_id == obj_id),
        "topic": select(Lesson.id).where(Lesson.topic_id == obj_id),
        "lesson": select(Quiz.id).where(Quiz.lesson_id == obj_id),
    }
    if await session.scalar(child_checks[kind].limit(1)) is not None:
        return True
    if kind == "lesson":
        opened = select(LessonAccess.id).where(LessonAccess.lesson_id == obj_id).limit(1)
        attempts = (
            select(QuizAttempt.id)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .where(Quiz.lesson_id == obj_id)
            .limit(1)
        )
        return (await session.scalar(opened)) is not None or (await session.scalar(attempts)) is not None
    return False


async def delete_node(session: AsyncSession, actor: User, kind: str, obj_id: int) -> None:
    """Delete only empty, unused items. Anything with children or student history is
    deactivated instead (results must be preserved, TZ §5.1.10, §8.4)."""
    obj = await get_node(session, kind, obj_id)
    await ensure_can_edit_node(session, actor, kind, obj)
    if await _has_children(session, kind, obj_id):
        raise Conflict(
            "Bu bo‘limda ichki ma’lumotlar yoki o‘quvchilar tarixi bor. O‘chirish o‘rniga uni nofaol qiling."
        )
    title = obj.title  # type: ignore[attr-defined]
    await _log(session, f"{kind}.deleted", actor, obj, title=title)
    await session.delete(obj)
    await session.flush()


# --------------------------------------------------------------------------- materials


async def attach_media(
    session: AsyncSession, lesson_id: int, media_id: int, *, actor: User, caption: str | None = None
) -> LessonMedia:
    lesson = await get_lesson(session, lesson_id)
    await ensure_can_manage_course(session, actor, lesson.course_id)
    if await session.get(MediaFile, media_id) is None:
        raise NotFound("Fayl topilmadi.")
    existing = await session.scalar(
        select(LessonMedia).where(LessonMedia.lesson_id == lesson_id, LessonMedia.media_id == media_id)
    )
    if existing is not None:
        return existing
    link = LessonMedia(
        lesson_id=lesson_id,
        media_id=media_id,
        caption=clean_text(caption, CAPTION_MAX),
        position=await _next_position(session, LessonMedia, LessonMedia.lesson_id, lesson_id),
    )
    session.add(link)
    await session.flush()
    await _log(session, "lesson.media_attached", actor, lesson, media_id=media_id)
    return link


async def detach_media(session: AsyncSession, lesson_id: int, media_id: int, *, actor: User) -> None:
    lesson = await get_lesson(session, lesson_id)
    await ensure_can_manage_course(session, actor, lesson.course_id)
    result = await session.execute(
        delete(LessonMedia).where(LessonMedia.lesson_id == lesson_id, LessonMedia.media_id == media_id)
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        raise NotFound()
    await _log(session, "lesson.media_detached", actor, lesson, media_id=media_id)


async def set_media_caption(
    session: AsyncSession, lesson_id: int, media_id: int, caption: str, *, actor: User
) -> None:
    lesson = await get_lesson(session, lesson_id)
    await ensure_can_manage_course(session, actor, lesson.course_id)
    await session.execute(
        update(LessonMedia)
        .where(and_(LessonMedia.lesson_id == lesson_id, LessonMedia.media_id == media_id))
        .values(caption=clean_text(caption, CAPTION_MAX))
    )
    await _log(session, "lesson.updated", actor, lesson, media_caption=media_id)


async def publish_lesson(session: AsyncSession, actor: User, lesson_id: int) -> Lesson:
    """Activate a lesson and announce it to the course audience (COMMERCE notifications)."""
    lesson = await get_lesson(session, lesson_id)
    await ensure_can_manage_course(session, actor, lesson.course_id)
    first_time = lesson.published_at is None
    lesson.is_active = True
    lesson.published_at = lesson.published_at or utcnow()
    await session.flush()
    await _log(session, "lesson.published", actor, lesson)
    if first_time:
        try:
            from app.services import notifications
        except ImportError:  # pragma: no cover - COMMERCE module not merged yet
            logger.warning("notifications service missing; new-lesson notice skipped")
        else:
            await notifications.notify_new_lesson(session, lesson.id)
    return lesson
