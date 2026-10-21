"""Media files, protected player and uploads (TZ §9). OWNER: COMMERCE.

- Protected video is never sent as a Telegram file: students get `player_button()`, a Mini App
  that plays AES-128 HLS with short-lived tokens (see app/web/routers/media.py).
- Non-video materials go out with `send_protected_document()` (protect_content=True).
- Large files are uploaded through the resumable upload page (`upload_button()`); small
  non-video files sent to the bot are stored with `store_telegram_file()`.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode

import redis.asyncio as aioredis
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, Message, WebAppInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import (
    AdminPermission,
    Lesson,
    LessonAccess,
    LessonMedia,
    MediaFile,
    MediaKind,
    MediaStatus,
    User,
    UserRole,
)
from app.services import storage
from app.services.access import has_permission, is_admin
from app.services.audit import log_action
from app.services.errors import AccessDenied, InvalidInput, NotFound, SubscriptionRequired
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

PART_SIZE = 8 * 1024 * 1024  # S3 minimum is 5 MiB for every part but the last
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile limit
UPLOAD_STATE_TTL = 7 * 24 * 3600  # resumable for a week; S3 lifecycle should abort older uploads

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


# --- validation ---------------------------------------------------------------------------


def file_extension(filename: str) -> str:
    return PurePosixPath(filename.lower()).suffix.lstrip(".")


def kind_for_filename(filename: str) -> MediaKind | None:
    ext = file_extension(filename)
    return next((kind for kind, exts in ALLOWED_EXTENSIONS.items() if ext in exts), None)


def sniff_matches(ext: str, head: bytes) -> bool:
    """Cheap magic-byte check of the first bytes against the declared extension."""
    if ext == "pdf":
        return head.startswith(b"%PDF")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {"jpg", "jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if ext == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext in {"mp4", "mov", "m4v", "m4a"}:
        return head[4:8] in {b"ftyp", b"moov", b"wide", b"mdat", b"free"}
    if ext in {"mkv", "webm"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext in {"docx", "pptx", "xlsx", "zip"}:
        return head.startswith(b"PK\x03\x04")
    if ext in {"doc", "ppt", "xls"}:
        return head.startswith(b"\xd0\xcf\x11\xe0")
    if ext == "mp3":
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext in {"ogg", "oga"}:
        return head.startswith(b"OggS")
    if ext == "wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == "aac":
        return len(head) > 1 and head[0] == 0xFF and head[1] & 0xF0 == 0xF0
    if ext == "txt":
        return b"\x00" not in head
    return False


def validate_upload(filename: str, size: int, kind: MediaKind | None = None) -> MediaKind:
    """Return the media kind or raise InvalidInput (format / size)."""
    detected = kind_for_filename(filename)
    if detected is None or (kind is not None and detected != kind):
        raise InvalidInput("Bu fayl formati qo‘llab-quvvatlanmaydi.")
    max_bytes = get_settings().media_max_upload_mb * 1024 * 1024
    if size <= 0 or size > max_bytes:
        raise InvalidInput(
            f"Fayl hajmi 0 dan katta va {get_settings().media_max_upload_mb} MB dan oshmasligi kerak."
        )
    return detected


def new_storage_key(kind: MediaKind, filename: str) -> str:
    ext = file_extension(filename) or "bin"
    return f"originals/{kind.value}/{utcnow():%Y/%m}/{uuid.uuid4().hex}.{ext}"


def safe_filename(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name.strip() or "file"
    return "".join(ch for ch in name if ch.isprintable())[:255]


# --- playback tokens ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediaToken:
    telegram_id: int
    media_id: int
    expires_at: int  # unix seconds
    teacher: bool = False  # granted through a teacher session: re-checked on every request
    subscriber: bool = False  # granted through a subscription: re-checked on playlist/key


@dataclass(frozen=True, slots=True)
class PlaybackGrant:
    """Result of `authorize_playback`: what may be played and until when at most."""

    media: MediaFile
    access_until: datetime | None = None  # end of the subscription / teacher session behind the grant
    teacher: bool = False
    subscriber: bool = False

    def ttl(self, requested: int, now: datetime) -> int:
        """Token lifetime capped at the end of the authorizing access."""
        if self.access_until is None:
            return requested
        return max(0, min(requested, int((self.access_until - now).total_seconds())))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload: bytes) -> bytes:
    key = hashlib.sha256(b"media-token:" + get_settings().secret_key.get_secret_value().encode()).digest()
    return hmac.new(key, payload, hashlib.sha256).digest()


def issue_token(
    telegram_id: int,
    media_id: int,
    *,
    ttl_seconds: int | None = None,
    now: float | None = None,
    teacher: bool = False,
    subscriber: bool = False,
) -> str:
    ttl = get_settings().media_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    issued = int(now if now is not None else utcnow().timestamp())
    data: dict[str, int] = {"u": telegram_id, "m": media_id, "x": issued + ttl}
    if teacher:
        data["t"] = 1
    if subscriber:
        data["s"] = 1
    payload = json.dumps(data, separators=(",", ":")).encode()
    return f"{_b64(payload)}.{_b64(_sign(payload))}"


def verify_token(token: str, *, now: float | None = None) -> MediaToken | None:
    """None if the token is malformed, tampered with or expired (expired tokens are never reusable)."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        if not hmac.compare_digest(_sign(payload), _unb64(sig_b64)):
            return None
        data = json.loads(payload)
        parsed = MediaToken(
            int(data["u"]), int(data["m"]), int(data["x"]), bool(data.get("t")), bool(data.get("s"))
        )
    except (ValueError, KeyError, TypeError):
        return None
    current = now if now is not None else utcnow().timestamp()
    return parsed if current < parsed.expires_at else None


# --- buttons ------------------------------------------------------------------------------


def _web_url(path: str, **params: Any) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return f"{get_settings().web_base_url.rstrip('/')}{path}" + (f"?{query}" if query else "")


def player_button(
    media: MediaFile, *, lesson_id: int, text: str = "▶️ Videoni ko‘rish"
) -> InlineKeyboardButton:
    """WebApp button opening the protected player (works in private chats only)."""
    return InlineKeyboardButton(
        text=text, web_app=WebAppInfo(url=_web_url("/media/player", media=media.id, lesson=lesson_id))
    )


def upload_button(*, lesson_id: int | None = None, text: str = "⬆️ Fayl yuklash") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, web_app=WebAppInfo(url=_web_url("/media/upload", lesson=lesson_id))
    )


# --- permissions --------------------------------------------------------------------------


async def _teacher_course_ids(session: AsyncSession, user: User, right: str) -> set[int]:
    """Courses where `user` is an assigned teacher with an ACTIVE 24h session (AUTH's services)."""
    if user.role != UserRole.TEACHER:
        return set()
    from app.services import teacher_sessions  # AUTH; imported lazily

    active = await teacher_sessions.get_active_session(session, user.telegram_id)
    if active is None:
        return set()
    return await teacher_sessions.teacher_course_ids(session, active.teacher_id, right=right)


async def require_media_manager(session: AsyncSession, user: User, lesson_id: int | None) -> None:
    """Admins with CONTENT, or teachers (active session) allowed to manage the lesson's course."""
    if has_permission(user, AdminPermission.CONTENT):
        return
    course_ids = await _teacher_course_ids(session, user, "can_manage_content")
    if not course_ids:
        raise AccessDenied()
    if lesson_id is not None:
        course_id = await session.scalar(select(Lesson.course_id).where(Lesson.id == lesson_id))
        if course_id not in course_ids:
            raise AccessDenied()


async def authorize_playback(
    session: AsyncSession, user: User, media_id: int, lesson_id: int | None
) -> PlaybackGrant:
    """Server-side access check before a playback token is issued (TZ §9.3).

    The media must be attached to the given lesson, so a free lesson can't be used to unlock
    another lesson's video. The grant carries the end of the access that authorized it
    (teacher session / subscription) so tokens never outlive it.
    """
    media = await session.get(MediaFile, media_id)
    if media is None or media.kind != MediaKind.VIDEO:
        raise NotFound()
    if has_permission(user, AdminPermission.CONTENT):
        return PlaybackGrant(_ready(media))
    if lesson_id is None:
        raise AccessDenied()
    linked = await session.scalar(
        select(LessonMedia.id).where(LessonMedia.lesson_id == lesson_id, LessonMedia.media_id == media_id)
    )
    lesson = await session.get(Lesson, lesson_id)
    if linked is None or lesson is None or not lesson.is_active:
        raise NotFound()
    from app.services import lessons  # CONTENT; imported lazily

    if user.role == UserRole.TEACHER and not is_admin(user):
        # Teacher media access is bound to the active 24h session.
        from app.services import teacher_sessions

        active = await teacher_sessions.get_active_session(session, user.telegram_id)
        if active is None:
            raise AccessDenied("O‘qituvchi sessiyasi tugagan. Iltimos, qayta kiring.")
        if not await lessons.can_view_lesson(session, user, lesson):
            raise AccessDenied()
        return PlaybackGrant(_ready(media), active.expires_at, teacher=True)

    if not await lessons.can_view_lesson(session, user, lesson):
        raise SubscriptionRequired()
    if (
        lesson.is_free
        and await lessons.is_lesson_public(session, lesson)
        and await _opened_free(session, user, lesson)
    ):
        return PlaybackGrant(_ready(media))  # free allowance: no end date
    from app.services.subscriptions import effective_end

    end = await effective_end(session, user.id)
    if end is None:  # only content staff reach here without a subscription
        raise SubscriptionRequired()
    return PlaybackGrant(_ready(media), end, subscriber=True)


async def _opened_free(session: AsyncSession, user: User, lesson: Lesson) -> bool:
    return bool(
        await session.scalar(
            select(LessonAccess.consumed_free_slot).where(
                LessonAccess.user_id == user.id, LessonAccess.lesson_id == lesson.id
            )
        )
    )


async def token_still_valid(
    session: AsyncSession, token: MediaToken, *, check_subscription: bool = True
) -> bool:
    """Re-check the access behind a token (teacher session revoked, subscription cancelled)."""
    if token.teacher:
        from app.services import teacher_sessions

        if await teacher_sessions.get_active_session(session, token.telegram_id) is None:
            return False
    if token.subscriber and check_subscription:
        from app.services.subscriptions import has_active_subscription

        user_id = await session.scalar(select(User.id).where(User.telegram_id == token.telegram_id))
        if user_id is None or not await has_active_subscription(session, user_id):
            return False
    return True


def _ready(media: MediaFile) -> MediaFile:
    if media.status != MediaStatus.READY or not media.hls_prefix or not media.hls_aes_key:
        raise NotFound("Video hali tayyor emas. Birozdan so‘ng qayta urinib ko‘ring.")
    return media


# --- Telegram files -----------------------------------------------------------------------


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
) -> MediaFile:
    """Copy a small non-video file sent to the bot into private storage. Video must use the upload page."""
    if kind == MediaKind.VIDEO:
        raise InvalidInput("Videoni bot orqali emas, «⬆️ Fayl yuklash» sahifasi orqali yuklang.")
    filename = safe_filename(filename)
    if size is not None and size > TELEGRAM_DOWNLOAD_LIMIT:
        raise InvalidInput("Fayl 20 MB dan katta. «⬆️ Fayl yuklash» sahifasidan foydalaning.")
    validate_upload(filename, size or 1, kind)

    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    data = buffer.getvalue()
    if not sniff_matches(file_extension(filename), data[:64]):
        raise InvalidInput("Fayl mazmuni uning formatiga mos emas.")

    key = new_storage_key(kind, filename)
    content_type = CONTENT_TYPES.get(file_extension(filename), mime_type or "application/octet-stream")
    await storage.put_bytes(key, data, content_type)
    media = MediaFile(
        kind=kind,
        status=MediaStatus.READY,
        storage_key=key,
        original_filename=filename,
        mime_type=content_type,
        size_bytes=len(data),
        checksum_sha256=hashlib.sha256(data).hexdigest(),
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
        details={"kind": kind.value, "size": len(data), "via": "telegram"},
    )
    return media


async def send_protected_document(
    bot: Bot, chat_id: int, media: MediaFile, *, caption: str | None = None
) -> Message:
    """Send a non-video material with protect_content=True (no forwarding / saving).

    Uses the cached Telegram file_id when available, else streams the file from storage and
    caches the new file_id on `media` (persisted by the caller's transaction).
    """
    if media.kind == MediaKind.VIDEO:
        raise InvalidInput("Video faqat himoyalangan player orqali ko‘rsatiladi.")
    if media.status != MediaStatus.READY:
        raise NotFound("Fayl hali tayyor emas.")
    send = bot.send_photo if media.kind == MediaKind.IMAGE else bot.send_document
    if media.telegram_file_id:
        try:
            if media.kind == MediaKind.IMAGE:
                return await bot.send_photo(
                    chat_id, media.telegram_file_id, caption=caption, protect_content=True
                )
            return await bot.send_document(
                chat_id, media.telegram_file_id, caption=caption, protect_content=True
            )
        except Exception:  # noqa: BLE001 - stale/foreign file_id: fall back to re-uploading
            logger.info("cached file_id failed for media %s, re-uploading", media.id)

    data = await storage.get_bytes(media.storage_key)
    upload = BufferedInputFile(data, filename=media.original_filename or f"material-{media.id}")
    message = await send(chat_id, upload, caption=caption, protect_content=True)
    if message.photo:
        media.telegram_file_id = message.photo[-1].file_id
    elif message.document:
        media.telegram_file_id = message.document.file_id
    return message


# --- resumable uploads --------------------------------------------------------------------

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def _upload_key(media_id: int) -> str:
    return f"media:upload:{media_id}"


@dataclass(frozen=True, slots=True)
class UploadState:
    media_id: int
    upload_id: str
    storage_key: str
    size: int
    part_size: int
    lesson_id: int | None
    user_id: int

    @property
    def total_parts(self) -> int:
        return max(1, math.ceil(self.size / self.part_size))

    def expected_part_size(self, part_number: int) -> int:
        if part_number < self.total_parts:
            return self.part_size
        return self.size - self.part_size * (self.total_parts - 1)


async def start_upload(
    session: AsyncSession, user: User, *, filename: str, size: int, lesson_id: int | None
) -> UploadState:
    await require_media_manager(session, user, lesson_id)
    filename = safe_filename(filename)
    kind = validate_upload(filename, size)
    if lesson_id is not None and await session.get(Lesson, lesson_id) is None:
        raise NotFound()
    key = new_storage_key(kind, filename)
    content_type = CONTENT_TYPES.get(file_extension(filename), "application/octet-stream")
    media = MediaFile(
        kind=kind,
        status=MediaStatus.UPLOADING,
        storage_key=key,
        original_filename=filename,
        mime_type=content_type,
        size_bytes=size,
        is_protected=True,
        uploaded_by_user_id=user.id,
    )
    session.add(media)
    await session.flush()
    upload_id = await storage.create_multipart(key, content_type)
    state = UploadState(media.id, upload_id, key, size, PART_SIZE, lesson_id, user.id)
    await get_redis().set(
        _upload_key(media.id),
        json.dumps(
            {
                "upload_id": upload_id,
                "size": size,
                "part_size": PART_SIZE,
                "lesson_id": lesson_id,
                "user_id": user.id,
            }
        ),
        ex=UPLOAD_STATE_TTL,
    )
    return state


async def get_upload(session: AsyncSession, user: User, media_id: int) -> UploadState:
    """Upload state, only for the user who started it and only while still uploading."""
    raw = await get_redis().get(_upload_key(media_id))
    media = await session.get(MediaFile, media_id)
    if raw is None or media is None or media.status != MediaStatus.UPLOADING:
        raise NotFound("Yuklash sessiyasi topilmadi yoki tugagan.")
    data = json.loads(raw)
    if data["user_id"] != user.id:
        raise AccessDenied()
    await require_media_manager(session, user, data["lesson_id"])
    return UploadState(
        media_id,
        data["upload_id"],
        media.storage_key,
        data["size"],
        data["part_size"],
        data["lesson_id"],
        user.id,
    )


async def put_part(state: UploadState, part_number: int, data: bytes, sha256_hex: str | None) -> str:
    if not 1 <= part_number <= state.total_parts:
        raise InvalidInput("Noto‘g‘ri qism raqami.")
    if len(data) != state.expected_part_size(part_number):
        raise InvalidInput("Qism hajmi noto‘g‘ri.")
    if sha256_hex is not None and not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(), sha256_hex.lower()
    ):
        raise InvalidInput("Qism checksum mos kelmadi, qayta yuboring.")
    if part_number == 1 and not sniff_matches(file_extension(state.storage_key), data[:64]):
        raise InvalidInput("Fayl mazmuni uning formatiga mos emas.")
    return await storage.upload_part(state.storage_key, state.upload_id, part_number, data)


async def uploaded_parts(state: UploadState) -> list[int]:
    return [p["PartNumber"] for p in await storage.list_parts(state.storage_key, state.upload_id)]


async def complete_upload(session: AsyncSession, user: User, state: UploadState) -> MediaFile:
    """Finish the multipart upload, attach to the lesson and queue processing.

    The worker computes the full-file SHA-256 (stored in checksum_sha256) and transcodes video.
    """
    parts = await storage.list_parts(state.storage_key, state.upload_id)
    if [p["PartNumber"] for p in parts] != list(range(1, state.total_parts + 1)):
        raise InvalidInput("Hamma qismlar yuklanmagan.")
    if sum(p["Size"] for p in parts) != state.size:
        raise InvalidInput("Fayl hajmi mos kelmadi.")
    await storage.complete_multipart(state.storage_key, state.upload_id, parts)
    await get_redis().delete(_upload_key(state.media_id))

    media = await session.get(MediaFile, state.media_id)
    assert media is not None
    media.status = MediaStatus.UPLOADED
    if state.lesson_id is not None:
        from app.services import lessons  # CONTENT

        await lessons.attach_media(session, state.lesson_id, media.id, actor=user)
    await log_action(
        session,
        "media.uploaded",
        actor=user,
        entity_type="media",
        entity_id=media.id,
        details={"kind": media.kind.value, "size": state.size, "lesson_id": state.lesson_id, "via": "web"},
    )
    return media


async def abort_upload(session: AsyncSession, state: UploadState) -> None:
    await storage.abort_multipart(state.storage_key, state.upload_id)
    await get_redis().delete(_upload_key(state.media_id))
    media = await session.get(MediaFile, state.media_id)
    if media is not None:
        media.status = MediaStatus.FAILED
        media.error = "cancelled"


async def enqueue_processing(media_id: int) -> None:
    """Queue checksum + (for video) HLS transcoding. Safe to call twice (job id dedupes)."""
    from app.workers.queue import get_arq_pool

    pool = await get_arq_pool()
    await pool.enqueue_job("process_media", media_id, _job_id=f"process_media:{media_id}")
