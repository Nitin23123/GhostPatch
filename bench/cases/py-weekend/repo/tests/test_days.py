from datetime import date

from calendar_utils.days import next_business_day


def test_monday_to_tuesday():
    assert next_business_day(date(2026, 9, 21)) == date(2026, 9, 22)
