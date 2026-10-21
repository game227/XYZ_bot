"""📚 Fanlar va kurslar — student catalog: Subject → Course → Module → Topic → Lessons.

Only active items whose whole parent chain is active are shown.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.student.lessons import lesson_label, lesson_marks
from app.keyboards import menus
from app.keyboards.content import CtLes, CtNav, btn, markup, show
from app.services import lessons
from app.services.errors import NotFound
from app.utils.text import esc, truncate

router = Router(name="student.catalog")

DESCRIPTION_PREVIEW = 1500
NOT_AVAILABLE = "Bu bo‘lim hozircha mavjud emas."


def _describe(title: str, description: str | None) -> str:
    text = f"<b>{esc(title)}</b>"
    if description:
        text += f"\n\n{esc(truncate(description, DESCRIPTION_PREVIEW))}"
    return text


async def render(
    session: AsyncSession, user: User, level: str, obj_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    rows: list[list[InlineKeyboardButton]] = []
    match level:
        case "subjects":
            subjects = await lessons.list_subjects(session)
            if not subjects:
                return "📚 Hozircha fanlar qo‘shilmagan.", None
            rows = [[btn(f"📗 {s.title}", CtNav(level="subject", id=s.id))] for s in subjects]
            return "📚 <b>Fanlar va kurslar</b>\n\nFanni tanlang:", markup(rows)

        case "subject":
            subject = await lessons.get_subject(session, obj_id)
            if not subject.is_active:
                raise NotFound(NOT_AVAILABLE)
            courses = await lessons.list_courses(session, subject.id)
            rows = [[btn(f"🎓 {c.title}", CtNav(level="course", id=c.id))] for c in courses]
            rows.append([btn("⬅️ Fanlar", CtNav(level="subjects"))])
            text = _describe(subject.title, subject.description)
            return text + ("\n\nKursni tanlang:" if courses else "\n\nBu fanda hali kurslar yo‘q."), markup(
                rows
            )

        case "course":
            course = await lessons.get_course(session, obj_id)
            subject = await lessons.get_subject(session, course.subject_id)
            if not (course.is_active and subject.is_active):
                raise NotFound(NOT_AVAILABLE)
            modules = await lessons.list_modules(session, course.id)
            rows = [[btn(f"📦 {m.title}", CtNav(level="module", id=m.id))] for m in modules]
            try:
                from app.services import chat  # AUTH

                rows.append([chat.write_to_teacher_button(course.id)])
            except ImportError:  # pragma: no cover
                pass
            rows.append([btn("⬅️ Kurslar", CtNav(level="subject", id=subject.id))])
            text = _describe(course.title, course.description)
            return text + (
                "\n\nModulni tanlang:" if modules else "\n\nBu kursda hali modullar yo‘q."
            ), markup(rows)

        case "module":
            module = await lessons.get_module(session, obj_id)
            course = await lessons.get_course(session, module.course_id)
            subject = await lessons.get_subject(session, course.subject_id)
            if not (module.is_active and course.is_active and subject.is_active):
                raise NotFound(NOT_AVAILABLE)
            topics = await lessons.list_topics(session, module.id)
            rows = [[btn(f"🗂 {t.title}", CtNav(level="topic", id=t.id))] for t in topics]
            rows.append([btn("⬅️ Modullar", CtNav(level="course", id=course.id))])
            text = _describe(module.title, module.description)
            return text + (
                "\n\nMavzuni tanlang:" if topics else "\n\nBu modulda hali mavzular yo‘q."
            ), markup(rows)

        case "topic":
            topic = await lessons.get_topic(session, obj_id)
            module = await lessons.get_module(session, topic.module_id)
            course = await lessons.get_course(session, module.course_id)
            subject = await lessons.get_subject(session, course.subject_id)
            if not (topic.is_active and module.is_active and course.is_active and subject.is_active):
                raise NotFound(NOT_AVAILABLE)
            items = await lessons.list_lessons(session, topic.id)
            subscriber, opened = await lesson_marks(session, user)
            rows = [
                [btn(lesson_label(lsn, subscriber, opened), CtLes(action="open", lesson_id=lsn.id))]
                for lsn in items
            ]
            rows.append([btn("⬅️ Mavzular", CtNav(level="module", id=module.id))])
            text = _describe(topic.title, topic.description)
            if items:
                text += "\n\nDarsni tanlang:\n🆓 bepul · 🔒 obuna kerak · ✅ siz ochgan bepul dars · 📖 ochiq"
            else:
                text += "\n\nBu mavzuda hali darslar yo‘q."
            return text, markup(rows)

    raise NotFound()


@router.message(F.text == menus.BTN_COURSES)
async def catalog_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render(session, user, "subjects", 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtNav.filter())
async def catalog_nav(
    callback: CallbackQuery, callback_data: CtNav, session: AsyncSession, user: User
) -> None:
    text, kb = await render(session, user, callback_data.level, callback_data.id)
    await show(callback, text, kb)
    await callback.answer()
