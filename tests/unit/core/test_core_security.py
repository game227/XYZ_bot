import logging

import pytest

from app.config import Settings, get_settings
from app.database.models import AdminPermission, User, UserRole, UserStatus
from app.services import access
from app.services.audit import _sanitize
from app.services.errors import AccessDenied
from app.utils.logging import RedactingFilter


def _user(role: UserRole = UserRole.STUDENT, telegram_id: int = 111, **kw: object) -> User:
    return User(telegram_id=telegram_id, role=role, status=kw.pop("status", UserStatus.ACTIVE), **kw)


@pytest.fixture
def superadmin(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("SUPERADMIN_IDS", "999, 1000")
    get_settings.cache_clear()
    yield 999
    get_settings.cache_clear()


def test_settings_parse_comma_separated_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERADMIN_IDS", "1, 2,3")
    monkeypatch.setenv("LIVE_REMINDER_MINUTES", "1440,60")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.superadmin_ids == [1, 2, 3]
    assert settings.live_reminder_minutes == [1440, 60]


def test_student_is_not_admin() -> None:
    student = _user()
    assert not access.is_admin(student)
    assert not access.has_permission(student, AdminPermission.USERS)
    with pytest.raises(AccessDenied):
        access.require_admin(student)


def test_admin_permissions_are_granular() -> None:
    admin = _user(UserRole.ADMIN, permissions=["content"])
    assert access.is_admin(admin)
    assert access.has_permission(admin, AdminPermission.CONTENT)
    assert not access.has_permission(admin, AdminPermission.PAYMENTS)
    access.require_admin(admin, AdminPermission.CONTENT)
    with pytest.raises(AccessDenied):
        access.require_admin(admin, AdminPermission.PAYMENTS)


def test_blocked_admin_loses_rights() -> None:
    admin = _user(UserRole.ADMIN, permissions=["content"], status=UserStatus.BLOCKED)
    assert not access.is_admin(admin)
    assert not access.has_permission(admin, AdminPermission.CONTENT)


def test_superadmin_has_every_permission(superadmin: int) -> None:
    root = _user(UserRole.STUDENT, telegram_id=superadmin, permissions=[])
    assert access.is_superadmin(root)
    assert all(access.has_permission(root, p) for p in AdminPermission)


def test_audit_details_never_keep_secrets() -> None:
    details = {
        "login": "teacher1",
        "password": "hunter2",
        "session_token": "abc",
        "nested": {"api_key": "k", "ok": 1},
        "items": [{"otp": "123456"}],
    }
    clean = _sanitize(details)
    assert clean["login"] == "teacher1"
    assert clean["password"] == "***"
    assert clean["session_token"] == "***"
    assert clean["nested"] == {"api_key": "***", "ok": 1}
    assert clean["items"] == [{"otp": "***"}]


def test_log_filter_redacts_secrets() -> None:
    token = "123456789:AAH-very-secret-telegram-bot-token-xyz"
    flt = RedactingFilter([token, "s3-secret-value"])
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "token=%s key %s password=qwerty", (token, "s3-secret-value"), None
    )
    flt.filter(record)
    message = record.getMessage()
    assert token not in message
    assert "s3-secret-value" not in message
    assert "qwerty" not in message
