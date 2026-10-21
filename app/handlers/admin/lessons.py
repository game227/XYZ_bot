"""🎬 Darslar va media (admin) + lesson materials screen (shared with teachers).

Materials (video up to 2000 MB, other files up to 200 MB) are sent straight to the bot and
kept as Telegram file_ids; students receive them with protect_content=True.
"""

from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, MediaKind, MediaStatus, User
from app.handlers.admin.content import render_courses
from app.keyboards import menus
from app.keyboards.content import CtEd, btn, markup, show
from app.services import lessons
from app.services.access import AdminFilter
from app.services.errors import InvalidInput
from app.utils.text import esc

router = Router(name="admin.lessons")

STATUS_NAMES = {
    MediaStatus.UPLOADING: "⏳ yuklanmoqda",
    MediaStatus.UPLOADED: "⏳ yuklandi",
    MediaStatus.PROCESSING: "⚙️ qayta ishlanmoqda",
    MediaStatus.READY: "✅ tayyor",
    MediaStatus.FAILED: "❌ xato",
}
KIND_NAMES = {
    MediaKind.VIDEO: "🎬 video",
    MediaKind.AUDIO: "🎧 audio",
    MediaKind.PDF: "📄 PDF",
    MediaKind.IMAGE: "🖼 rasm",
    MediaKind.DOCUMENT: "📎 hujjat",
}


class CtLessonFile(StatesGroup):
    waiting = State()


@router.message(AdminFilter(AdminPermission.CONTENT), F.text == menus.BTN_A_LESSONS_MEDIA)
async def lessons_media_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render_courses(session, user)
    await message.answer(text, reply_markup=kb)


async def render_materials(
    session: AsyncSession, user: User, lesson_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    lesson = await lessons.get_lesson(session, lesson_id)
    await lessons.ensure_can_manage_course(session, user, lesson.course_id)
    items = await lessons.lesson_materials(session, lesson.id, ready_only=False)
    lines = [f"📎 <b>Materiallar: {esc(lesson.title)}</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for n, (link, mf) in enumerate(items, start=1):
        name = link.caption or mf.original_filename or f"#{mf.id}"
        status = STATUS_NAMES.get(MediaStatus(mf.status), str(mf.status))
        lines.append(f"{n}. {KIND_NAMES.get(MediaKind(mf.kind), mf.kind)} — {esc(name)} ({status})")
        rows.append([btn(f"❌ {n}. {name}", CtEd(action="detach", kind="lesson", id=lesson.id, extra=mf.id))])
    if not items:
        lines.append("Hozircha materiallar yo‘q.")
    lines.append(
        "\nVideo (2000 MB gacha), PDF, hujjat, rasm yoki audio (200 MB gacha) — botga yuboring. "
        "O‘quvchilarga ular himoyalangan holda (forward va saqlash yopiq) yuboriladi."
    )
    rows.append([btn("📨 Material yuborish", CtEd(action="addfile", kind="lesson", id=lesson.id))])
    rows.append([btn("🔄 Yangilash", CtEd(action="media", kind="lesson", id=lesson.id))])
    rows.append([btn("⬅️ Darsga qaytish", CtEd(action="view", kind="lesson", id=lesson.id))])
    return "\n".join(lines), markup(rows)


@router.callback_query(CtEd.filter(F.action == "media"))
async def materials(callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User) -> None:
    text, kb = await render_materials(session, user, callback_data.id)
    await show(callback, text, kb)
    await callback.answer()


@router.callback_query(CtEd.filter(F.action == "detach"))
async def detach(callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User) -> None:
    await lessons.detach_media(session, callback_data.id, callback_data.extra, actor=user)
    text, kb = await render_materials(session, user, callback_data.id)
    await show(callback, text, kb)
    await callback.answer("Material darsdan olib tashlandi")


@router.callback_query(CtEd.filter(F.action == "addfile"))
async def ask_file(
    callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User, state: FSMContext
) -> None:
    lesson = await lessons.get_lesson(session, callback_data.id)
    await lessons.ensure_can_manage_course(session, user, lesson.course_id)
    await state.set_state(CtLessonFile.waiting)
    await state.update_data(ct_lesson=lesson.id)
    await callback.answer()
    await callback.bot.send_message(  # type: ignore[union-attr]
        callback.from_user.id,
        "Video (2000 MB gacha) yoki PDF, hujjat, rasm, audio (200 MB gacha) yuboring. "
        "Izoh (caption) material nomi bo‘ladi.\n\nBekor qilish: /menu",
    )


@dataclass(slots=True)
class SentFile:
    file_id: str
    file_unique_id: str
    kind: MediaKind
    filename: str
    mime_type: str | None
    size: int | None
    duration: int | None = None


def _describe_file(message: Message) -> SentFile:
    """The attachment of `message`, or InvalidInput."""
    from app.services import media  # COMMERCE

    if message.photo:
        photo = message.photo[-1]
        name = f"photo-{photo.file_unique_id}.jpg"
        return SentFile(
            photo.file_id, photo.file_unique_id, MediaKind.IMAGE, name, "image/jpeg", photo.file_size
        )
    video = message.video or message.animation
    if video is not None:
        name = getattr(video, "file_name", None) or f"video-{video.file_unique_id}.mp4"
        return SentFile(
            video.file_id,
            video.file_unique_id,
            MediaKind.VIDEO,
            name,
            video.mime_type,
            video.file_size,
            video.duration,
        )
    doc = message.document or message.audio or message.voice
    if doc is None:
        raise InvalidInput("Fayl yuboring (video, PDF, hujjat, rasm yoki audio).")
    filename = getattr(doc, "file_name", None) or ("voice.ogg" if message.voice else "file")
    kind = media.kind_for_filename(filename)
    if kind is None:
        raise InvalidInput("Bu fayl formati qo‘llab-quvvatlanmaydi.")
    return SentFile(
        doc.file_id,
        doc.file_unique_id,
        kind,
        filename,
        doc.mime_type,
        doc.file_size,
        getattr(doc, "duration", None),
    )


@router.message(CtLessonFile.waiting, F.photo | F.document | F.audio | F.voice | F.video | F.animation)
async def receive_file(
    message: Message, bot: Bot, session: AsyncSession, user: User, state: FSMContext
) -> None:
    lesson_id = int((await state.get_data()).get("ct_lesson") or 0)
    lesson = await lessons.get_lesson(session, lesson_id)
    await lessons.ensure_can_manage_course(session, user, lesson.course_id)
    sent = _describe_file(message)

    from app.services import media  # COMMERCE

    stored = await media.store_telegram_file(
        bot,
        session,
        file_id=sent.file_id,
        kind=sent.kind,
        filename=sent.filename,
        mime_type=sent.mime_type,
        size=sent.size,
        actor=user,
        file_unique_id=sent.file_unique_id,
        duration=sent.duration,
    )
    await lessons.attach_media(session, lesson.id, stored.id, actor=user, caption=message.caption)
    await state.clear()
    text, kb = await render_materials(session, user, lesson.id)
    await message.answer(f"✅ Material qo‘shildi.\n\n{text}", reply_markup=kb)
