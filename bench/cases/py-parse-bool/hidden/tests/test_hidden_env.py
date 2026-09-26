from config.env import parse_bool
from config.settings import debug_enabled


def test_false_values():
    for v in ('false', '0', 'no', 'off', 'OFF', 'False'):
        assert parse_bool(v) is False, v


def test_true_values():
    for v in ('true', '1', 'yes', 'on', 'Yes'):
        assert parse_bool(v) is True, v


def test_debug():
    assert debug_enabled({'DEBUG': 'false'}) is False
    assert debug_enabled({}) is False
