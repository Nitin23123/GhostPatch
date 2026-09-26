from settings.loader import load


def test_top_level_override():
    assert load({"debug": True})["debug"] is True
