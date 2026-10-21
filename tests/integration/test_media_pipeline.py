"""End-to-end media pipeline: upload API → process_media (ffmpeg, AES-128 HLS) → MinIO → protected player.

Needs PostgreSQL (TEST_DATABASE_URL), MinIO (S3 settings) and ffmpeg; skipped otherwise.
Uses its own bucket, because test media ids restart at 1 and would collide with dev objects (hls/<id>/).
"""

import asyncio
import hashlib
import itertools
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database.models import Course, Lesson, MediaFile, MediaStatus, Module, Subject, Topic, User, UserRole
from app.services import media, notifications, storage, subscriptions
from app.web.app import app
from app.workers import jobs
from tests.teacher_helpers import sign_init_data

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]
_ids = itertools.count(72_000_001)
TEST_BUCKET = f"xyz-media-test-{uuid.uuid4().hex[:8]}"


def _minio_up() -> bool:
    try:
        return httpx.get(f"{get_settings().s3_endpoint_url}/minio/health/live", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
async def test_bucket(monkeypatch):
    if not _minio_up():
        pytest.skip("MinIO is not reachable")
    monkeypatch.setattr(storage, "_bucket", lambda: TEST_BUCKET)
    await storage.ensure_bucket()
    yield TEST_BUCKET
    await storage.delete_prefix("")
    async with storage.s3_client() as s3:
        await s3.delete_bucket(Bucket=TEST_BUCKET)


@pytest.fixture(autouse=True)
def no_queue(monkeypatch):
    async def noop(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(notifications, "enqueue_send", noop)
    monkeypatch.setattr(media, "enqueue_processing", noop)  # the job is run inline below


async def _make_video(path: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=15",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440",
        "-t",
        "3",
        "-pix_fmt",
        "yuv420p",
        "-y",
        str(path),
    )
    assert await proc.wait() == 0


def _hdr(telegram_id: int) -> dict[str, str]:
    return {"X-Telegram-Init-Data": sign_init_data(telegram_id)}


async def test_upload_transcode_and_protected_playback(committing_sessionmaker, test_bucket, tmp_path):
    async with committing_sessionmaker() as s:
        admin = User(telegram_id=next(_ids), first_name="Admin", role=UserRole.ADMIN, permissions=["content"])
        allowed = User(telegram_id=next(_ids), first_name="Obunachi")
        stranger = User(telegram_id=next(_ids), first_name="Begona")
        s.add_all([admin, allowed, stranger])
        await s.flush()
        subject = Subject(title="Fan")
        s.add(subject)
        await s.flush()
        course = Course(subject_id=subject.id, title="Kurs")
        s.add(course)
        await s.flush()
        module = Module(course_id=course.id, title="Modul")
        s.add(module)
        await s.flush()
        topic = Topic(module_id=module.id, title="Mavzu")
        s.add(topic)
        await s.flush()
        lesson = Lesson(topic_id=topic.id, course_id=course.id, title="Pullik dars", is_free=False)
        s.add(lesson)
        await s.flush()
        await subscriptions.grant_subscription(s, admin, allowed.id, 30)
        await s.commit()

    video = tmp_path / "dars.mp4"
    await _make_video(video)
    data = video.read_bytes()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Resumable upload as admin (single part: the clip is < 8 MiB).
        r = await client.post(
            "/media/api/uploads",
            json={"filename": "dars.mp4", "size": len(data), "lesson_id": lesson.id},
            headers=_hdr(admin.telegram_id),
        )
        assert r.status_code == 200, r.text
        info = r.json()
        media_id = info["media_id"]
        assert info["total_parts"] == 1
        r = await client.put(
            f"/media/api/uploads/{media_id}/parts/1",
            content=data,
            headers={**_hdr(admin.telegram_id), "X-Part-Sha256": hashlib.sha256(data).hexdigest()},
        )
        assert r.status_code == 200, r.text
        r = await client.post(f"/media/api/uploads/{media_id}/complete", headers=_hdr(admin.telegram_id))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "uploaded"

        # 2. Worker job (inline): checksum + ffmpeg → AES-128 HLS in MinIO.
        assert await jobs.process_media({}, media_id) == "ready"
        async with committing_sessionmaker() as s:
            item = await s.get(MediaFile, media_id)
            assert item is not None and item.status == MediaStatus.READY
            assert item.checksum_sha256 == hashlib.sha256(data).hexdigest()
            assert item.hls_prefix == f"hls/{media_id}" and item.hls_aes_key and len(item.hls_aes_key) == 16
            linked = await s.scalar(select(Lesson.id).where(Lesson.id == lesson.id))
            assert linked is not None
        assert await storage.head(f"hls/{media_id}/index.m3u8") is not None
        assert await storage.head(f"hls/{media_id}/seg_00000.ts") is not None

        # 3. Allowed student: token → playlist, key and a segment.
        r = await client.post(
            "/media/api/play",
            json={"media_id": media_id, "lesson_id": lesson.id},
            headers=_hdr(allowed.telegram_id),
        )
        assert r.status_code == 200, r.text
        play = r.json()
        assert play["watermark"] == f"ID {allowed.telegram_id}"
        base = play["playlist_url"].rsplit("/", 1)[0]
        r = await client.get(play["playlist_url"])
        assert r.status_code == 200 and "#EXT-X-KEY:METHOD=AES-128" in r.text
        segment = next(line for line in r.text.splitlines() if line.endswith(".ts"))
        r = await client.get(f"{base}/key")
        assert r.status_code == 200 and len(r.content) == 16
        r = await client.get(f"{base}/{segment}")
        assert r.status_code == 200 and len(r.content) > 0

        # 4. A student without a subscription gets nothing.
        r = await client.post(
            "/media/api/play",
            json={"media_id": media_id, "lesson_id": lesson.id},
            headers=_hdr(stranger.telegram_id),
        )
        assert r.status_code == 403
        # ...and can't swap in another lesson id either.
        r = await client.post(
            "/media/api/play",
            json={"media_id": media_id, "lesson_id": lesson.id + 999},
            headers=_hdr(stranger.telegram_id),
        )
        assert r.status_code == 404

        # 5. Subscription cancelled: the already-issued token stops opening playlist and key.
        async with committing_sessionmaker() as s:
            actor = await s.get(User, admin.id)
            assert actor is not None
            assert await subscriptions.cancel_subscriptions(s, actor, allowed.id) == 1
            await s.commit()
        assert (await client.get(play["playlist_url"])).status_code == 403
        assert (await client.get(f"{base}/key")).status_code == 403
        r = await client.post(
            "/media/api/play",
            json={"media_id": media_id, "lesson_id": lesson.id},
            headers=_hdr(allowed.telegram_id),
        )
        assert r.status_code == 403
