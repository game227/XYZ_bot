"""Student lesson screen, materials and 🎓 Bepul darslar (TZ §4, §5, §9.1).

Protected video is never sent as a Telegram file: the lesson shows a WebApp button to
COMMERCE's player. Other materials go out with protect_content=True.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import Lesson, MediaKind, User
from app.keyboards import menus
from app.keyboards.content import PAGE_SIZE, CtLes, CtList, CtNav, CtQz, btn, chunks, markup, pager_row, show
from app.services import lessons, quizzes
from app.services.errors import AccessDenied
from app.services.subscriptions import has_active_subscription
from app.utils.text import esc

logger = logging.getLogger(__name__)

router = Router(name="student.lessons")

KIND_ICONS = {
    MediaKind.PDF: "📄",
    MediaKind.AUDIO: "🎧",
    MediaKind.IMAGE: "🖼",
    MediaKind.DOCUMENT: "📎",
}


async def lesson_marks(session: AsyncSession, user: User) -> tuple[bool, set[int]]:
    """(is_subscriber, free lesson ids already opened) — for 🔒/🆓/✅ marks in lists."""
    return await has_active_subscription(session, user.id), await lessons.opened_free_lesson_ids(
        session, user.id
    )


def lesson_label(lesson: Lesson, subscriber: bool, opened_free: set[int]) -> str:
    if subscriber or lesson.id in opened_free:
        mark = "✅" if lesson.id in opened_free else "📖"
    elif lesson.is_free:
        mark = "🆓"
    else:
        mark = "🔒"
    return f"{mark} {lesson.title}"


async def _send_lesson(
    bot: Bot, session: AsyncSession, user: User, chat_id: int, lesson: Lesson, note: str | None
) -> None:
    path = await lessons.lesson_path(session, lesson)
    header = (
        f"<i>{esc(path.subject.title)} › {esc(path.course.title)} › {esc(path.module.title)} › "
        f"{esc(path.topic.title)}</i>\n\n<b>📘 {esc(lesson.title)}</b>"
    )
    if lesson.short_description:
        header += f"\n{esc(lesson.short_description)}"
    if note:
        header = f"{note}\n\n{header}"

    rows: list[list[InlineKeyboardButton]] = []
    materials = await lessons.lesson_materials(session, lesson.id)
    videos = [(lm, mf) for lm, mf in materials if mf.kind == MediaKind.VIDEO]
    others = [(lm, mf) for lm, mf in materials if mf.kind != MediaKind.VIDEO]
    if videos:
        from app.services import media  # COMMERCE

        for n, (link, mf) in enumerate(videos, start=1):
            label = link.caption or ("Videoni ko‘rish" if len(videos) == 1 else f"Video {n}")
            rows.append([media.player_button(mf, lesson_id=lesson.id, text=f"▶️ {label}")])
    for n, (link, mf) in enumerate(others, start=1):
        label = link.caption or mf.original_filename or f"Material {n}"
        icon = KIND_ICONS.get(MediaKind(mf.kind), "📎")
        rows.append([btn(f"{icon} {label}", CtLes(action="mat", lesson_id=lesson.id, media_id=mf.id))])
    for quiz in await quizzes.list_lesson_quizzes(session, lesson.id):
        rows.append([btn(f"🧪 {quiz.title}", CtQz(action="card", quiz_id=quiz.id))])
    try:
        from app.services import chat  # AUTH

        rows.append([chat.write_to_teacher_button(lesson.course_id)])
    except ImportError:  # pragma: no cover
        pass
    rows.append([btn("⬅️ Mavzuga qaytish", CtNav(level="topic", id=lesson.topic_id))])

    parts = chunks(lesson.body_text or "")
    first = header + (f"\n\n{esc(parts[0])}" if parts else "")
    rest = [esc(p) for p in parts[1:]]
    if not rest:
        await bot.send_message(chat_id, first, reply_markup=markup(rows))
        return
    await bot.send_message(chat_id, first)
    for i, part in enumerate(rest):
        await bot.send_message(chat_id, part, reply_markup=markup(rows) if i == len(rest) - 1 else None)


async def _open_and_send(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, user: User, lesson: Lesson
) -> None:
    result = await lessons.open_lesson(session, user, lesson)
    # Release the users-row lock before talking to Telegram.
    await session.commit()
    note = None
    if result.consumed_free_slot:
        note = f"🎓 Bepul dars ochildi. Qolgan bepul darslar: <b>{result.free_left}</b>."
    await callback.answer()
    await _send_lesson(bot, session, user, callback.from_user.id, lesson, note)


@router.callback_query(CtLes.filter(F.action == "open"))
async def open_lesson(
    callback: CallbackQuery, callback_data: CtLes, bot: Bot, session: AsyncSession, user: User
) -> None:
    lesson = await lessons.get_lesson(session, callback_data.lesson_id)
    if (
        lesson.is_free
        and await lessons.is_lesson_public(session, lesson)
        and not await lessons.can_view_lesson(session, user, lesson)
    ):
        left = await lessons.free_lessons_left(session, user)
        if left > 0:
            # Opening consumes one of the free lessons: ask first (TZ §5.1.4).
            text = (
                f"🎓 <b>{esc(lesson.title)}</b> — bepul dars.\n\n"
                f"Uni ochsangiz, bepul darslar limitingizdan 1 tasi ishlatiladi "
                f"(qolgan: <b>{left}</b>). Keyin bu darsni istalgancha qayta ochishingiz mumkin."
            )
            kb = markup(
                [
                    [btn("✅ Ochish", CtLes(action="confirm", lesson_id=lesson.id))],
                    [btn("⬅️ Orqaga", CtNav(level="topic", id=lesson.topic_id))],
                ]
            )
            await show(callback, text, kb)
            await callback.answer()
            return
    await _open_and_send(callback, bot, session, user, lesson)


@router.callback_query(CtLes.filter(F.action == "confirm"))
async def confirm_open(
    callback: CallbackQuery, callback_data: CtLes, bot: Bot, session: AsyncSession, user: User
) -> None:
    lesson = await lessons.get_lesson(session, callback_data.lesson_id)
    await _open_and_send(callback, bot, session, user, lesson)


@router.callback_query(CtLes.filter(F.action == "mat"))
async def send_material(
    callback: CallbackQuery, callback_data: CtLes, bot: Bot, session: AsyncSession, user: User
) -> None:
    lesson = await lessons.get_lesson(session, callback_data.lesson_id)
    if not await lessons.can_view_lesson(session, user, lesson):
        raise AccessDenied("Bu materialni ko‘rish uchun avval darsni oching.")
    media_file = await lessons.get_lesson_material(session, lesson.id, callback_data.media_id)
    if media_file.kind == MediaKind.VIDEO:
        await callback.answer("Video faqat himoyalangan player orqali ko‘rsatiladi.", show_alert=True)
        return
    from app.services import media  # COMMERCE

    await callback.answer("Yuborilmoqda…")
    await media.send_protected_document(bot, callback.from_user.id, media_file, caption=esc(lesson.title))


# --------------------------------------------------------------------------- 🎓 free lessons


async def render_free(
    session: AsyncSession, user: User, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    items, total = await lessons.list_free_lessons(session, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    subscriber, opened = await lesson_marks(session, user)
    if subscriber:
        head = "🎓 <b>Bepul darslar</b>\n\nObunangiz faol — barcha darslar siz uchun ochiq."
    else:
        left = await lessons.free_lessons_left(session, user)
        head = (
            "🎓 <b>Bepul darslar</b>\n\n"
            f"Obunasiz jami {get_settings().free_lesson_limit} ta bepul darsni ochish mumkin. "
            f"Qolgan: <b>{left}</b>. Ochilgan darslar (✅) har doim qayta ochiladi."
        )
    if not items:
        return head + "\n\nHozircha bepul darslar yo‘q.", None
    rows = [
        [btn(lesson_label(lsn, subscriber, opened), CtLes(action="open", lesson_id=lsn.id))] for lsn in items
    ]
    rows.append(pager_row(page, total, CtList, kind="free"))
    return head, markup(rows)


@router.message(F.text == menus.BTN_FREE_LESSONS)
async def free_lessons_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render_free(session, user, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtList.filter(F.kind == "free"))
async def free_lessons_page(
    callback: CallbackQuery, callback_data: CtList, session: AsyncSession, user: User
) -> None:
    text, kb = await render_free(session, user, max(0, callback_data.page))
    await show(callback, text, kb)
    await callback.answer()
