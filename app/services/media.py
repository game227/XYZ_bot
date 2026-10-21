"""Lesson materials kept on Telegram's servers (TZ §9, bot-only mode). OWNER: COMMERCE.

Admins and teachers send a video (up to 2000 MB) or a document / PDF / image / audio (up to
200 MB) straight to the bot. Only the Telegram file_id and metadata are stored; files are
never downloaded, so the Bot API 20 MB download limit does not apply. Students receive the
material with protect_content=True, which blocks forwarding and saving in Telegram clients.
"""

import logging
import uuid
from pathlib import PurePosixPath

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import MediaFile, MediaKind, MediaStatus, User
from app.services.audit import log_action
from app.services.errors import InvalidInput, NotFound

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS: dict[MediaKind, set[str]] = {
    MediaKind.VIDEO: {"mp4", "mov", "m4v", "mkv", "webm"},
    MediaKind.AUDIO: {"mp3", "m4a", "aac", "ogg", "oga", "wav"},
    MediaKind.PDF: {"pdf"},
    MediaKind.IMAGE: {"jpg", "jpeg", "png", "webp"},
    MediaKind.DOCUMENT: {"doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "zip"},
}

CONTENT_TYPES = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "wav": "audio/wav",
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "doc": "application/msword",
    "ppt": "application/vnd.ms-powerpoint",
    "xls": "application/vnd.ms-excel",
    "txt": "text/plain",
    "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def file_extension(filename: str) -> str:
    return PurePosixPath(filename.lower()).suffix.lstrip(".")


def kind_for_filename(filename: str) -> MediaKind | None:
    ext = file_extension(filename)
    return next((kind for kind, exts in ALLOWED_EXTENSIONS.items() if ext in exts), None)


def safe_filename(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name.strip() or "file"
    return "".join(ch for ch in name if ch.isprintable())[:255]


def size_limit_mb(kind: MediaKind) -> int:
    settings = get_settings()
    return settings.telegram_video_max_mb if kind == MediaKind.VIDEO else settings.telegram_document_max_mb


async def store_telegram_file(
    bot: Bot,
    session: AsyncSession,
    *,
    file_id: str,
    kind: MediaKind,
    filename: str,
    mime_type: str | None,
    size: int | None,
    actor: User,
    file_unique_id: str | None = None,
    duration: int | None = None,
) -> MediaFile:
    """Register a material sent to the bot (metadata + Telegram file_id only)."""
    filename = safe_filename(filename)
    ext = file_extension(filename)
    if kind != MediaKind.VIDEO and kind_for_filename(filename) is None:
        raise InvalidInput("Bu fayl formati qo‘llab-quvvatlanmaydi.")
    limit_mb = size_limit_mb(kind)
    if size is not None and size > limit_mb * 1024 * 1024:
        raise InvalidInput(f"Fayl hajmi {limit_mb} MB dan oshmasligi kerak.")

    media = MediaFile(
        kind=kind,
        status=MediaStatus.READY,
        storage_key=f"telegram/{kind.value}/{file_unique_id or uuid.uuid4().hex}.{ext or 'bin'}",
        original_filename=filename,
        mime_type=mime_type or CONTENT_TYPES.get(ext, "application/octet-stream"),
        size_bytes=size,
        duration_seconds=duration,
        is_protected=True,
        telegram_file_id=file_id,
        uploaded_by_user_id=actor.id,
    )
    session.add(media)
    await session.flush()
    await log_action(
        session,
        "media.uploaded",
        actor=actor,
        entity_type="media",
        entity_id=media.id,
        details={"kind": kind.value, "size": size},
    )
    return media


async def send_protected_document(
    bot: Bot, chat_id: int, media: MediaFile, *, caption: str | None = None
) -> Message:
    """Send any material (video, audio, image, PDF, document) with protect_content=True."""
    if media.status != MediaStatus.READY or not media.telegram_file_id:
        raise NotFound("Fayl topilmadi. Uni botga qayta yuklash kerak.")
    file = media.telegram_file_id
    if media.kind == MediaKind.VIDEO:
        return await bot.send_video(
            chat_id, file, caption=caption, protect_content=True, supports_streaming=True
        )
    if media.kind == MediaKind.IMAGE:
        return await bot.send_photo(chat_id, file, caption=caption, protect_content=True)
    if media.kind == MediaKind.AUDIO:
        return await bot.send_audio(chat_id, file, caption=caption, protect_content=True)
    return await bot.send_document(chat_id, file, caption=caption, protect_content=True)
