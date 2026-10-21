import time

from app.services import media


def test_token_roundtrip():
    token = media.issue_token(555, 42, ttl_seconds=600)
    parsed = media.verify_token(token)
    assert parsed is not None
    assert (parsed.telegram_id, parsed.media_id) == (555, 42)


def test_expired_token_is_rejected_and_not_reusable():
    issued_at = time.time() - 601
    token = media.issue_token(555, 42, ttl_seconds=600, now=issued_at)
    assert media.verify_token(token) is None
    # Still valid just before expiry, invalid exactly at expiry.
    assert media.verify_token(token, now=issued_at + 599) is not None
    assert media.verify_token(token, now=issued_at + 600) is None


def test_tampered_token_is_rejected():
    token = media.issue_token(555, 42)
    payload, sig = token.split(".")
    other_payload = media.issue_token(555, 43).split(".")[0]
    assert media.verify_token(f"{other_payload}.{sig}") is None  # swapped media id
    assert media.verify_token(f"{payload}.{sig[:-2]}AA") is None
    assert media.verify_token("garbage") is None
    assert media.verify_token("") is None
    assert media.verify_token("a.b.c") is None


def test_player_button_is_webapp_with_ids():
    item = media.MediaFile(id=7)
    button = media.player_button(item, lesson_id=3)
    assert button.web_app is not None
    assert button.web_app.url.endswith("/media/player?media=7&lesson=3")
