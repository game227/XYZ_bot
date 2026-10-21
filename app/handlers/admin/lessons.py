"""🎬 Darslar va media (admin) + lesson materials screen (shared with teachers).

Videos and big files go through COMMERCE's WebApp upload page (resumable, checksum,
then HLS transcoding). Small non-video files can be sent straight to the bot; they are
copied into private storage and attached to the lesson.
"""

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
        "\nVideo va katta fayllar uchun «⬆️ Fayl yuklash» sahifasidan foydalaning. "
        "Video yuklangach avtomatik qayta ishlanadi va faqat himoyalangan player orqali ko‘rsatiladi.\n"
        "PDF, rasm, audio yoki hujjat (20 MB gacha) — bot orqali ham yuborish mumkin."
    )
    from app.services import media  # COMMERCE

    rows.append([media.upload_button(lesson_id=lesson.id)])
    rows.append([btn("📨 Faylni bot orqali yuborish", CtEd(action="addfile", kind="lesson", id=lesson.id))])
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
        "PDF, rasm, audio yoki hujjatni yuboring (20 MB gacha). Izoh (caption) material nomi bo‘ladi.\n"
        "Video bu yerda qabul qilinmaydi — «⬆️ Fayl yuklash» sahifasidan foydalaning.\n\nBekor qilish: /menu",
    )


def _describe_file(message: Message) -> tuple[str, MediaKind, str, str | None, int | None]:
    """(file_id, kind, filename, mime_type, size) of the attachment, or InvalidInput."""
    from app.services import media  # COMMERCE

    if message.photo:
        photo = message.photo[-1]
        return (
            photo.file_id,
            MediaKind.IMAGE,
            f"photo-{photo.file_unique_id}.jpg",
            "image/jpeg",
            photo.file_size,
        )
    if message.video or message.video_note or message.animation:
        raise InvalidInput("Video bu yerda qabul qilinmaydi — «⬆️ Fayl yuklash» sahifasidan foydalaning.")
    doc = message.document or message.audio or message.voice
    if doc is None:
        raise InvalidInput("Fayl yuboring (PDF, rasm, audio yoki hujjat).")
    filename = getattr(doc, "file_name", None) or ("voice.ogg" if message.voice else "file")
    kind = media.kind_for_filename(filename)
    if kind is None:
        raise InvalidInput("Bu fayl formati qo‘llab-quvvatlanmaydi.")
    if kind == MediaKind.VIDEO:
        raise InvalidInput("Video bu yerda qabul qilinmaydi — «⬆️ Fayl yuklash» sahifasidan foydalaning.")
    return doc.file_id, kind, filename, doc.mime_type, doc.file_size


@router.message(CtLessonFile.waiting, F.photo | F.document | F.audio | F.voice | F.video | F.animation)
async def receive_file(
    message: Message, bot: Bot, session: AsyncSession, user: User, state: FSMContext
) -> None:
    lesson_id = int((await state.get_data()).get("ct_lesson") or 0)
    lesson = await lessons.get_lesson(session, lesson_id)
    await lessons.ensure_can_manage_course(session, user, lesson.course_id)
    file_id, kind, filename, mime_type, size = _describe_file(message)

    from app.services import media  # COMMERCE

    stored = await media.store_telegram_file(
        bot,
        session,
        file_id=file_id,
        kind=kind,
        filename=filename,
        mime_type=mime_type,
        size=size,
        actor=user,
    )
    await lessons.attach_media(session, lesson.id, stored.id, actor=user, caption=message.caption)
    await state.clear()
    text, kb = await render_materials(session, user, lesson.id)
    await message.answer(f"✅ Material qo‘shildi.\n\n{text}", reply_markup=kb)
