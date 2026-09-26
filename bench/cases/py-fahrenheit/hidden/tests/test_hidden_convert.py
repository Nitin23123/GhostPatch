from units.convert import c_to_f, f_to_c
from units.weather import report


def test_boiling_and_freezing():
    assert f_to_c(212) == 100
    assert f_to_c(32) == 0


def test_round_trip():
    assert abs(f_to_c(c_to_f(37)) - 37) < 1e-9


def test_report():
    assert report('Oslo', 50) == 'Oslo: 10°C'
