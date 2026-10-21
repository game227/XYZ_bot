from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import notifications, subscriptions
from app.services.errors import InvalidInput

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_period_starts_now_without_coverage():
    assert subscriptions.compute_period(NOW, None, 30) == (NOW, NOW + timedelta(days=30))


def test_renewal_stacks_on_current_end():
    current_end = NOW + timedelta(days=5)
    starts, ends = subscriptions.compute_period(NOW, current_end, 30)
    assert starts == current_end and ends == current_end + timedelta(days=30)


def test_expired_coverage_does_not_shift_start():
    starts, _ = subscriptions.compute_period(NOW, NOW - timedelta(days=1), 30)
    assert starts == NOW


@pytest.mark.parametrize(
    ("delta", "kind"),
    [
        (timedelta(days=5), None),
        (timedelta(days=3), notifications.KIND_SUB_EXPIRING_3D),
        (timedelta(days=2), notifications.KIND_SUB_EXPIRING_3D),
        (timedelta(hours=23), notifications.KIND_SUB_EXPIRING_1D),
        (timedelta(seconds=-1), notifications.KIND_SUB_EXPIRED),
        (timedelta(days=-3), None),  # too old for an expiry notice
    ],
)
def test_classify_reminder(delta, kind):
    assert subscriptions.classify_reminder(NOW + delta, NOW) == kind


def test_format_remaining():
    assert subscriptions.format_remaining(timedelta(days=2, hours=5)) == "2 kun 5 soat"
    assert subscriptions.format_remaining(timedelta(days=3)) == "3 kun"
    assert subscriptions.format_remaining(timedelta(hours=1, minutes=30)) == "1 soat 30 daqiqa"
    assert subscriptions.format_remaining(timedelta(seconds=20)) == "1 daqiqa"
    assert subscriptions.format_remaining(timedelta(0)) == "tugagan"


def test_parse_price_and_days():
    assert subscriptions.parse_price("150 000") == Decimal("150000.00")
    assert subscriptions.parse_price("99,5") == Decimal("99.50")
    for bad in ("abc", "-5", "0", "NaN", "1e20"):
        with pytest.raises(InvalidInput):
            subscriptions.parse_price(bad)
    assert subscriptions.parse_days(" 30 ") == 30
    for bad in ("0", "x", "99999"):
        with pytest.raises(InvalidInput):
            subscriptions.parse_days(bad)


def test_format_price():
    assert subscriptions.format_price(Decimal("150000.00")) == "150 000 so‘m"
    assert subscriptions.format_price(Decimal("9.50"), "USD") == "9.50 USD"
