from config.env import parse_bool


def test_true():
    assert parse_bool("true") is True
