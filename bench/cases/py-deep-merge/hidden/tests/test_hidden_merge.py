import copy

from settings import loader
from settings.merge import merge


def test_nested_values_keep_their_defaults():
    assert loader.load({"db": {"port": 6543}}) == {
        "db": {"host": "localhost", "port": 6543, "options": {"timeout": 30, "ssl": False}},
        "debug": False,
    }


def test_three_levels_deep():
    assert loader.load({"db": {"options": {"ssl": True}}})["db"]["options"] == {"timeout": 30, "ssl": True}


def test_defaults_are_never_modified():
    before = copy.deepcopy(loader.DEFAULTS)
    loader.load({"db": {"port": 1, "options": {"timeout": 1}}})
    assert loader.DEFAULTS == before


def test_non_dict_values_replace_sections():
    assert merge({"a": {"b": 1}}, {"a": None}) == {"a": None}
