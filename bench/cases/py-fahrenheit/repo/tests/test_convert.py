from units.convert import c_to_f


def test_c_to_f():
    assert c_to_f(100) == 212
