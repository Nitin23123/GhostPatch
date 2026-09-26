from notes.note import Note
from notes.tags import add_tag


def test_notes_have_independent_tags():
    first = Note("call the bank").tag("urgent")
    second = Note("buy milk")
    assert first.tags == ["new", "urgent"]
    assert second.tags == ["new"]


def test_add_tag_starts_fresh_each_time():
    assert add_tag("x") == ["x"]
    assert add_tag("y") == ["y"]


def test_the_callers_list_is_still_updated():
    tags = ["a"]
    assert add_tag("b", tags) == ["a", "b"]
