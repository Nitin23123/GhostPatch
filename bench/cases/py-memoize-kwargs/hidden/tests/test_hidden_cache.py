from utils.cache import memoize
from utils.prices import price


def test_currency_counts():
    assert price("book") == 10.0
    assert price("book", currency="EUR") == 9.0


def test_same_kwargs_are_cached():
    calls = []

    @memoize
    def f(a, b=0, c=0):
        calls.append(1)
        return a + b + c

    assert f(1, b=2, c=3) == 6 and f(1, c=3, b=2) == 6
    assert len(calls) == 1
    assert f(1, b=5) == 6 and len(calls) == 2
