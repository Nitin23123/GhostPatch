from datetime import date

from booking.nights import nights


def test_nights_is_a_whole_number():
    assert isinstance(nights(date(2024, 6, 1), date(2024, 6, 3)), int)
