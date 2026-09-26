from app.options import DEFAULTS, with_overrides


def test_no_leak():
    with_overrides({'theme': 'dark'})
    assert with_overrides({})['theme'] == 'light'
    assert DEFAULTS == {'theme': 'light', 'font_size': 12}
