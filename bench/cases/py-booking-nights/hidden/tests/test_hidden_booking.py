from datetime import date

from booking.nights import nights
from booking.price import stay_price


def test_two_nights():
    assert nights(date(2024, 6, 1), date(2024, 6, 3)) == 2


def test_across_months():
    assert nights(date(2024, 6, 30), date(2024, 7, 2)) == 2


def test_price():
    assert stay_price(date(2024, 6, 1), date(2024, 6, 3), 100) == 200
