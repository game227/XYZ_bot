"""arq jobs. Each job opens its own DB sessions; `ctx["bot"]` is a shared aiogram Bot."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import update

from app.config import get_settings
from app.database.models import MediaFile, MediaKind, MediaStatus
from app.database.session import get_sessionmaker, session_scope
from app.services import live, notifications, storage, subscriptions
from app.services.media import file_extension
from app.utils.text import esc
from app.utils.time import utcnow
from app.workers.transcode import PLAYLIST, TranscodeError, sha256_file, transcode_to_hls

logger = logging.getLogger(__name__)

HLS_CONTENT_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}
UPLOAD_CONCURRENCY = 8


# --- media --------------------------------------------------------------------------------


async def process_media(ctx: dict[str, Any], media_id: int) -> str:
    """Full-file checksum for every upload; video additionally → AES-128 HLS in `hls/<id>/`."""
    async with session_scope() as session:
        claimed = (
            await session.execute(
                update(MediaFile)
                .where(
                    MediaFile.id == media_id,
                    MediaFile.status.in_([MediaStatus.UPLOADED, MediaStatus.PROCESSING, MediaStatus.FAILED]),
                )
                .values(status=MediaStatus.PROCESSING, error=None)
                .returning(
                    MediaFile.storage_key,
                    MediaFile.kind,
                    MediaFile.uploaded_by_user_id,
                    MediaFile.original_filename,
                )
            )
        ).one_or_none()
    if claimed is None:
        return "skipped"
    storage_key, kind, uploader_id, filename = claimed
    settings = get_settings()
    try:
        with tempfile.TemporaryDirectory(prefix=f"media{media_id}-") as tmp:
            work = Path(tmp)
            src = work / f"source.{file_extension(storage_key) or 'bin'}"
            await storage.download_file(storage_key, src)
            checksum = await asyncio.to_thread(sha256_file, src)
            values: dict[str, Any] = {"checksum_sha256": checksum, "status": MediaStatus.READY, "error": None}
            if kind == MediaKind.VIDEO:
                result = await transcode_to_hls(
                    settings.ffmpeg_bin, src, work, segment_seconds=settings.hls_segment_seconds
                )
                prefix = f"hls/{media_id}"
                await storage.delete_prefix(prefix + "/")
                await _upload_dir(result.out_dir, prefix)
                values |= {
                    "hls_prefix": prefix,
                    "hls_aes_key": result.aes_key,
                    "duration_seconds": result.duration_seconds,
                }
    except Exception as exc:  # noqa: BLE001 - recorded on the row, never shown with details to users
        logger.exception("media %s processing failed", media_id)
        message = str(exc)[:300] if isinstance(exc, TranscodeError) else type(exc).__name__
        async with session_scope() as session:
            await session.execute(
                update(MediaFile)
                .where(MediaFile.id == media_id)
                .values(status=MediaStatus.FAILED, error=message)
            )
            if uploader_id:
                await notifications.notify_user(
                    session,
                    uploader_id,
                    notifications.KIND_MEDIA_READY,
                    f"⚠️ «{esc(filename)}» faylini qayta ishlab bo‘lmadi. "
                    "Boshqa formatda qayta yuklab ko‘ring.",
                    dedupe_key=f"media_failed:{media_id}:{utcnow():%Y%m%d%H%M}",
                )
        return "failed"

    async with session_scope() as session:
        await session.execute(update(MediaFile).where(MediaFile.id == media_id).values(**values))
        if uploader_id and kind == MediaKind.VIDEO:
            await notifications.notify_user(
                session,
                uploader_id,
                notifications.KIND_MEDIA_READY,
                f"🎬 «{esc(filename)}» videosi tayyor va darsda ko‘rish mumkin.",
                dedupe_key=f"media_ready:{media_id}",
            )
    return "ready"


async def _upload_dir(out_dir: Path, prefix: str) -> None:
    files = await asyncio.to_thread(
        lambda: sorted(p for p in out_dir.iterdir() if p.is_file() and p.name != PLAYLIST)
    )
    semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    async def put(path: Path) -> None:
        async with semaphore:
            await storage.upload_file(
                f"{prefix}/{path.name}", path, HLS_CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            )

    await asyncio.gather(*(put(p) for p in files))
    # Playlist last: it only becomes visible once every segment exists.
    await storage.upload_file(f"{prefix}/{PLAYLIST}", out_dir / PLAYLIST, HLS_CONTENT_TYPES[".m3u8"])


# --- notifications ------------------------------------------------------------------------


async def send_notification(ctx: dict[str, Any], notification_id: int) -> str | None:
    status = await notifications.deliver(ctx["bot"], get_sessionmaker(), notification_id, ctx["limiter"])
    return status.value if status else None


async def sweep_notifications(ctx: dict[str, Any]) -> int:
    """Safety net: re-queue notifications whose enqueue was lost (Redis hiccup, crash)."""
    async with session_scope() as session:
        ids = await notifications.stuck_notification_ids(session, utcnow())
    for notification_id in ids:
        await notifications.enqueue_send(notification_id)
    return len(ids)


async def subscription_reminders(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        return await subscriptions.queue_due_reminders(session)


async def live_reminders(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        return await live.queue_due_reminders(session, get_settings().live_reminder_minutes)
