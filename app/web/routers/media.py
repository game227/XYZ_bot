"""Protected video player + resumable uploads (TZ §9). OWNER: COMMERCE.

Player flow:
1. The bot shows a WebApp button → GET /media/player?media=&lesson= (static page).
2. The page POSTs initData to /media/api/play; the server checks lesson access and returns a
   10-minute token; the page refreshes it before expiry (access is re-checked every time).
3. hls.js loads /media/hls/<token>/index.m3u8; the playlist references `key` and segments
   relatively, so every request carries the token. Expired/forged tokens get 403.
   The AES key is only served here; segments are proxied (dev) or 307-redirected to 60 s
   presigned URLs (prod) — they are encrypted either way.
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import MediaFile, User
from app.services import media, storage
from app.services.errors import AccessDenied, Conflict, InvalidInput, NotFound, ServiceError
from app.utils.time import utcnow
from app.web.deps import get_db
from app.web.telegram_auth import webapp_user
from app.workers.transcode import KEY_URI, PLAYLIST

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["media"])

STATIC = Path(__file__).resolve().parent.parent / "static"
SEGMENT_RE = re.compile(r"^seg_\d{5}\.ts$")
NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
NATIVE_TOKEN_MAX = 4 * 3600

DbSession = Annotated[AsyncSession, Depends(get_db)]
WebUser = Annotated[User, Depends(webapp_user)]


@contextmanager
def service_errors() -> Iterator[None]:
    """Map service errors to HTTP errors with the Uzbek user message (no technical details)."""
    try:
        yield
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.user_message) from None
    except AccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.user_message) from None
    except InvalidInput as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.user_message) from None
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.user_message) from None
    except ServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.user_message) from None


# --- pages --------------------------------------------------------------------------------


@router.get("/player", include_in_schema=False)
async def player_page() -> FileResponse:
    return FileResponse(STATIC / "player" / "index.html", headers=NO_STORE)


@router.get("/upload", include_in_schema=False)
async def upload_page() -> FileResponse:
    return FileResponse(STATIC / "upload" / "index.html", headers=NO_STORE)


# --- playback -----------------------------------------------------------------------------


class PlayRequest(BaseModel):
    media_id: int
    lesson_id: int | None = None
    native: bool = False  # Safari/iOS native HLS can't rotate tokens mid-playback


class PlayResponse(BaseModel):
    token: str
    playlist_url: str
    expires_in: int
    watermark: str
    title: str | None


@router.post("/api/play", response_model=PlayResponse)
async def play(
    body: PlayRequest,
    session: DbSession,
    user: WebUser,
) -> PlayResponse:
    with service_errors():
        grant = await media.authorize_playback(session, user, body.media_id, body.lesson_id)
    item = grant.media
    ttl = get_settings().media_token_ttl_seconds
    if body.native:
        ttl = min(max(ttl, (item.duration_seconds or 0) + ttl), NATIVE_TOKEN_MAX)
    # Never outlive the subscription / teacher session that authorized playback.
    ttl = grant.ttl(ttl, utcnow())
    if ttl <= 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ruxsat muddati tugagan.")
    token = media.issue_token(
        user.telegram_id, item.id, ttl_seconds=ttl, teacher=grant.teacher, subscriber=grant.subscriber
    )
    logger.info("playback token issued media=%s user=%s ttl=%s", item.id, user.telegram_id, ttl)
    return PlayResponse(
        token=token,
        playlist_url=f"/media/hls/{token}/{PLAYLIST}",
        expires_in=ttl,
        watermark=f"ID {user.telegram_id}",
        title=item.original_filename,
    )


def _token_or_403(token: str) -> media.MediaToken:
    parsed = media.verify_token(token)
    if parsed is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Havola muddati tugagan.")
    return parsed


async def _valid_token_or_403(session: AsyncSession, token: str, *, full_check: bool) -> media.MediaToken:
    """Signature/expiry always; the access behind the token is re-checked from the DB:
    teacher sessions on every request (TZ §7.4), subscriptions on playlist and key."""
    parsed = _token_or_403(token)
    if (parsed.teacher or parsed.subscriber) and not await media.token_still_valid(
        session, parsed, check_subscription=full_check
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ruxsat bekor qilingan.")
    return parsed


@router.get("/hls/{token}/" + PLAYLIST, include_in_schema=False)
async def playlist(token: str, session: DbSession) -> Response:
    parsed = await _valid_token_or_403(session, token, full_check=True)
    try:
        data = await storage.get_bytes(f"hls/{parsed.media_id}/{PLAYLIST}")
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    return Response(data, media_type="application/vnd.apple.mpegurl", headers=NO_STORE)


@router.get("/hls/{token}/" + KEY_URI, include_in_schema=False)
async def hls_key(token: str, session: DbSession) -> Response:
    parsed = await _valid_token_or_403(session, token, full_check=True)
    item = await session.get(MediaFile, parsed.media_id)
    if item is None or item.hls_aes_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(item.hls_aes_key, media_type="application/octet-stream", headers=NO_STORE)


@router.get("/hls/{token}/{segment}", include_in_schema=False)
async def hls_segment(token: str, segment: str, session: DbSession) -> Response:
    parsed = await _valid_token_or_403(session, token, full_check=False)
    if not SEGMENT_RE.match(segment):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    key = f"hls/{parsed.media_id}/{segment}"
    if get_settings().media_segment_mode == "redirect":
        return RedirectResponse(await storage.presigned_get(key), status_code=307, headers=NO_STORE)
    try:
        data = await storage.get_bytes(key)
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    return Response(data, media_type="video/mp2t", headers=NO_STORE)


# --- resumable uploads --------------------------------------------------------------------


class UploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    lesson_id: int | None = None


class UploadInfo(BaseModel):
    media_id: int
    part_size: int
    total_parts: int
    uploaded_parts: list[int] = []
    status: str = "uploading"


@router.post("/api/uploads", response_model=UploadInfo)
async def start_upload(body: UploadStart, session: DbSession, user: WebUser) -> UploadInfo:
    with service_errors():
        state = await media.start_upload(
            session, user, filename=body.filename, size=body.size, lesson_id=body.lesson_id
        )
    return UploadInfo(media_id=state.media_id, part_size=state.part_size, total_parts=state.total_parts)


@router.get("/api/uploads/{media_id}", response_model=UploadInfo)
async def upload_status(media_id: int, session: DbSession, user: WebUser) -> UploadInfo:
    with service_errors():
        state = await media.get_upload(session, user, media_id)
        parts = await media.uploaded_parts(state)
    return UploadInfo(
        media_id=media_id, part_size=state.part_size, total_parts=state.total_parts, uploaded_parts=parts
    )


@router.put("/api/uploads/{media_id}/parts/{part_number}")
async def upload_part(
    media_id: int,
    part_number: int,
    request: Request,
    session: DbSession,
    user: WebUser,
    x_part_sha256: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    with service_errors():
        state = await media.get_upload(session, user, media_id)
        length = int(request.headers.get("content-length") or 0)
        if length > state.part_size:
            raise InvalidInput("Qism hajmi juda katta.")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > state.part_size:
                raise InvalidInput("Qism hajmi juda katta.")
        await media.put_part(state, part_number, bytes(data), x_part_sha256)
    return {"part_number": part_number}


@router.post("/api/uploads/{media_id}/complete", response_model=UploadInfo)
async def complete_upload(media_id: int, session: DbSession, user: WebUser) -> UploadInfo:
    with service_errors():
        state = await media.get_upload(session, user, media_id)
        item = await media.complete_upload(session, user, state)
    await session.commit()  # the worker must see the row before it runs
    try:
        await media.enqueue_processing(item.id)
    except Exception:  # noqa: BLE001 - row stays UPLOADED; an admin can re-trigger from the bot
        logger.warning("could not enqueue processing for media %s", item.id, exc_info=True)
    return UploadInfo(
        media_id=item.id, part_size=state.part_size, total_parts=state.total_parts, status=item.status.value
    )


@router.delete("/api/uploads/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def abort_upload(media_id: int, session: DbSession, user: WebUser) -> Response:
    with service_errors():
        state = await media.get_upload(session, user, media_id)
        await media.abort_upload(session, state)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MediaStatusOut(BaseModel):
    media_id: int
    status: str
    kind: str
    error: str | None


@router.get("/api/media/{media_id}", response_model=MediaStatusOut)
async def media_status(media_id: int, session: DbSession, user: WebUser) -> MediaStatusOut:
    item = await session.get(MediaFile, media_id)
    if item is None or item.uploaded_by_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return MediaStatusOut(media_id=item.id, status=item.status.value, kind=item.kind.value, error=item.error)
