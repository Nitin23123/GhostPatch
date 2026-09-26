from stats.core import count, mean


def test_count():
    assert count([1, 2, 3]) == 3
    assert count([]) == 0


def test_mean_still_right():
    assert mean([2, 4, 6]) == 4
    assert mean([5]) == 5
