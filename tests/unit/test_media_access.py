"""Playback tokens never outlive the access behind them, and revoked access stops playback
on the next request (TZ §7.3-7.4, §9.3). No DB: services and storage are faked."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import media
from app.web.app import app
from app.web.deps import get_db

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


# --- TTL cap ------------------------------------------------------------------------------


def test_ttl_capped_at_subscription_or_session_end():
    grant = media.PlaybackGrant(media.MediaFile(id=1), NOW + timedelta(minutes=3), subscriber=True)
    assert grant.ttl(600, NOW) == 180
    assert grant.ttl(4 * 3600, NOW) == 180  # native-HLS tokens too
    assert grant.ttl(600, NOW + timedelta(minutes=5)) == 0


def test_ttl_uncapped_for_admins_and_free_lessons():
    assert media.PlaybackGrant(media.MediaFile(id=1)).ttl(600, NOW) == 600


def test_token_carries_access_flags():
    teacher = media.verify_token(media.issue_token(1, 2, teacher=True))
    sub = media.verify_token(media.issue_token(1, 2, subscriber=True))
    plain = media.verify_token(media.issue_token(1, 2))
    assert teacher is not None and teacher.teacher and not teacher.subscriber
    assert sub is not None and sub.subscriber and not sub.teacher
    assert plain is not None and not plain.teacher and not plain.subscriber


# --- re-check on playlist / key / segments ------------------------------------------------


class FakeSession:
    async def scalar(self, *_):
        return 77  # users.id for the subscriber lookup

    async def get(self, *_):
        return SimpleNamespace(hls_aes_key=b"k" * 16)


@pytest.fixture
def client(monkeypatch):
    async def fake_db():
        yield FakeSession()

    async def fake_get_bytes(key: str) -> bytes:
        return b"#EXTM3U\n" if key.endswith(".m3u8") else b"segment"

    monkeypatch.setattr(media.storage, "get_bytes", fake_get_bytes)
    app.dependency_overrides[get_db] = fake_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def teacher_active(monkeypatch):
    state = {"active": True}

    async def fake_get_active_session(session, telegram_id, **_):
        return object() if state["active"] else None

    import app.services.teacher_sessions as ts

    monkeypatch.setattr(ts, "get_active_session", fake_get_active_session)
    return state


@pytest.fixture
def subscribed(monkeypatch):
    state = {"active": True}

    async def fake_has_active(session, user_id, **_):
        return state["active"]

    import app.services.subscriptions as subs

    monkeypatch.setattr(subs, "has_active_subscription", fake_has_active)
    return state


def _paths(token: str) -> list[str]:
    return [f"/media/hls/{token}/index.m3u8", f"/media/hls/{token}/key", f"/media/hls/{token}/seg_00000.ts"]


def test_revoked_teacher_session_blocks_every_request(client, teacher_active):
    token = media.issue_token(500, 9, teacher=True)
    assert [client.get(p).status_code for p in _paths(token)] == [200, 200, 200]
    teacher_active["active"] = False  # admin revoked / /logout / 24h passed
    assert [client.get(p).status_code for p in _paths(token)] == [403, 403, 403]


def test_cancelled_subscription_blocks_playlist_and_key(client, subscribed):
    token = media.issue_token(500, 9, subscriber=True)
    assert [client.get(p).status_code for p in _paths(token)] == [200, 200, 200]
    subscribed["active"] = False
    playlist, key, _segment = (client.get(p).status_code for p in _paths(token))
    # Without the key the (AES-128 encrypted) segments are useless; segments themselves are
    # not re-checked to keep them DB-free, and the token itself ends with the subscription.
    assert (playlist, key) == (403, 403)


def test_expired_or_forged_token_is_403(client):
    expired = media.issue_token(500, 9, ttl_seconds=600, now=datetime.now(UTC).timestamp() - 601)
    assert [client.get(p).status_code for p in _paths(expired)] == [403, 403, 403]
    forged = media.issue_token(500, 9).split(".")[0] + ".AAAA"
    assert client.get(f"/media/hls/{forged}/index.m3u8").status_code == 403


def test_segment_name_is_validated(client):
    token = media.issue_token(500, 9)
    assert client.get(f"/media/hls/{token}/..%2F..%2Fsecret").status_code == 404
    assert client.get(f"/media/hls/{token}/enc.key").status_code == 404
