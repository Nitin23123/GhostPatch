from calc import add, percent


def test_add():
    assert add(2, 3) == 5


def test_percent():
    assert percent(1, 4) == 25
