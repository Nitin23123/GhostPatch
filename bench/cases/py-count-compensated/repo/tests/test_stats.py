from stats.core import mean


def test_mean():
    assert mean([2, 4, 6]) == 4
