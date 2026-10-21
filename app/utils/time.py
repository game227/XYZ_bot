from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def utcnow() -> datetime:
    """Timezone-aware UTC now. All datetimes are stored in UTC."""
    return datetime.now(UTC)


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz())


def fmt_dt(dt: datetime | None) -> str:
    """User-facing date+time in the bot's timezone, e.g. 21.10.2026 14:05."""
    return to_local(dt).strftime("%d.%m.%Y %H:%M") if dt else "—"


def fmt_date(dt: datetime | None) -> str:
    return to_local(dt).strftime("%d.%m.%Y") if dt else "—"
