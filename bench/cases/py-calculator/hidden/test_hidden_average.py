import pytest

from calc import average


def test_average_includes_every_number():
    assert average([2, 4, 6]) == 4
    assert average([10]) == 10
    assert average([-1, 1]) == 0


def test_empty_list_is_still_an_error():
    with pytest.raises(ValueError):
        average([])
