"""AUTH unit tests (no DB): password hashing/policy, tokens, login rate limiter, Mini App page."""

import pytest

from app.services import auth, teacher_sessions
from app.services.errors import InvalidInput
from tests.teacher_helpers import make_limiter, use_cheap_hasher


@pytest.fixture(autouse=True)
def cheap_hasher(monkeypatch):
    use_cheap_hasher(monkeypatch)


async def test_password_hash_is_argon2id_and_verifies() -> None:
    h = await auth.hash_password("Secret-pass-123")
    assert h.startswith("$argon2id$")
    assert "Secret-pass-123" not in h
    assert await auth.verify_password(h, "Secret-pass-123")
    assert not await auth.verify_password(h, "secret-pass-123")
    assert not await auth.verify_password("not-a-hash", "x")


def test_password_policy() -> None:
    auth.validate_new_password("Abcdefgh12", login="ali")
    for bad in ["short1", "onlyletterslong", "1234567890123", " Leading-space1", "x" * 129 + "1"]:
        with pytest.raises(InvalidInput):
            auth.validate_new_password(bad)
    with pytest.raises(InvalidInput):
        auth.validate_new_password("my-ali.login-2026", login="ali.login")


def test_login_validation() -> None:
    assert auth.validate_login("  Ali.Valiyev_1 ") == "ali.valiyev_1"
    for bad in ["ab", "a b c", "_start", "x" * 33, "ali@mail", ""]:
        with pytest.raises(InvalidInput):
            auth.validate_login(bad)


def test_temp_password_is_random_and_valid() -> None:
    pwds = {auth.generate_temp_password() for _ in range(50)}
    assert len(pwds) == 50
    for p in pwds:
        auth.validate_new_password(p)


def test_session_tokens_unguessable_and_only_hash_stored() -> None:
    tokens = {teacher_sessions.new_token() for _ in range(100)}
    assert len(tokens) == 100
    t = next(iter(tokens))
    assert len(t) >= 43  # 32 random bytes, urlsafe base64
    h = teacher_sessions.hash_token(t)
    assert len(h) == 64 and t not in h
    assert teacher_sessions.token_hint(t) == t[:4] + "…"


async def test_rate_limit_locks_after_five_failures_per_login() -> None:
    limiter, redis = make_limiter()
    subjects = limiter.subjects("Teacher1", 111)
    for _ in range(4):
        await limiter.check(subjects)
        assert not await limiter.register_failure(subjects)
    assert await limiter.register_failure(subjects)  # 5th failure locks
    with pytest.raises(auth.LoginLocked) as exc:
        await limiter.check(subjects)
    assert 0 < exc.value.retry_after <= auth.LOGIN_LOCK_SECONDS
    # Another Telegram account guessing the same login is locked too (per-login subject).
    with pytest.raises(auth.LoginLocked):
        await limiter.check(limiter.subjects("teacher1", 222))
    redis.advance(auth.LOGIN_LOCK_SECONDS + 1)
    await limiter.check(subjects)


async def test_rate_limit_per_telegram_id_across_logins() -> None:
    limiter, _ = make_limiter()
    for i in range(5):
        await limiter.register_failure(limiter.subjects(f"guess{i}", 333))
    with pytest.raises(auth.LoginLocked):
        await limiter.check(limiter.subjects("other", 333))


async def test_rate_limit_window_expires() -> None:
    limiter, redis = make_limiter()
    subjects = limiter.subjects("t", 1)
    for _ in range(4):
        await limiter.register_failure(subjects)
    redis.advance(auth.LOGIN_WINDOW_SECONDS + 1)
    assert not await limiter.register_failure(subjects)  # counter restarted
    await limiter.check(subjects)


async def test_rate_limit_reset_on_success() -> None:
    limiter, _ = make_limiter()
    subjects = limiter.subjects("t", 1)
    for _ in range(4):
        await limiter.register_failure(subjects)
    await limiter.reset(subjects)
    assert not await limiter.register_failure(subjects)


async def test_rate_limit_fails_closed_when_redis_down() -> None:
    limiter, redis = make_limiter()
    redis.fail = True
    with pytest.raises(auth.LoginUnavailable):
        await limiter.check(limiter.subjects("t", 1))
    with pytest.raises(auth.LoginUnavailable):
        await limiter.register_failure(limiter.subjects("t", 1))


def test_rate_limit_keys_do_not_contain_raw_login() -> None:
    subjects = auth.LoginRateLimiter.subjects("secret.login", 5)
    assert all("secret" not in s for s in subjects)


async def test_teacher_page_served_with_security_headers() -> None:
    import httpx

    from app.web.app import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/teacher/")
        assert resp.status_code == 200
        assert "O‘qituvchi kabineti" in resp.text
        assert resp.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in resp.headers["content-security-policy"]
        # API without Telegram initData is rejected before touching the DB.
        assert (await client.get("/teacher/api/threads")).status_code == 401
        bad = await client.get("/teacher/api/threads", headers={"X-Telegram-Init-Data": "user=1&hash=00"})
        assert bad.status_code == 401
