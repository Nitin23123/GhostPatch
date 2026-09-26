from utils.prices import price


def test_price():
    assert price("pen") == 2.0
