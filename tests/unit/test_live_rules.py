from datetime import UTC, datetime, timedelta

import pytest

from app.services import live
from app.services.errors import InvalidInput

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("until_start", "expected"),
    [
        (timedelta(hours=30), None),
        (timedelta(hours=23), 1440),
        (timedelta(minutes=59), 60),  # created late: only the nearest reminder
        (timedelta(minutes=-1), None),
    ],
)
def test_due_offset(until_start, expected):
    assert live.due_offset(NOW + until_start, NOW, [1440, 60]) == expected


def test_parse_local_datetime_is_tashkent_time():
    value = live.parse_local_datetime("25.10.2026 18:30", now=NOW)
    assert value == datetime(2026, 10, 25, 13, 30, tzinfo=UTC)  # UTC+5


def test_parse_local_datetime_rejects_past_and_garbage():
    with pytest.raises(InvalidInput):
        live.parse_local_datetime("01.01.2020 10:00", now=NOW)
    with pytest.raises(InvalidInput):
        live.parse_local_datetime("ertaga", now=NOW)


def test_validate_url():
    assert live.validate_url(" https://meet.example/abc ") == "https://meet.example/abc"
    for bad in ("javascript:alert(1)", "meet.example", "https://a b"):
        with pytest.raises(InvalidInput):
            live.validate_url(bad)
