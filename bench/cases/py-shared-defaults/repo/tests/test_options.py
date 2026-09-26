from app.options import with_overrides


def test_override():
    assert with_overrides({"theme": "dark"})["theme"] == "dark"
