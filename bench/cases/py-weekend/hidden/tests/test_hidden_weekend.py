from datetime import date

from calendar_utils.days import is_weekend, next_business_day
from calendar_utils.shipping import delivery_date


def test_saturday_and_sunday_are_weekend_days():
    assert is_weekend(date(2026, 9, 26))  # Saturday
    assert is_weekend(date(2026, 9, 27))  # Sunday
    assert not is_weekend(date(2026, 9, 25))  # Friday


def test_friday_rolls_over_to_monday():
    assert next_business_day(date(2026, 9, 25)) == date(2026, 9, 28)


def test_delivery_skips_the_weekend():
    assert delivery_date(date(2026, 9, 24), 2) == date(2026, 9, 28)  # Thursday + 2 business days
