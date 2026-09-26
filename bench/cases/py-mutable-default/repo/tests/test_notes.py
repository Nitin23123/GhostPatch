from notes.tags import add_tag


def test_no_duplicate_tags():
    assert add_tag("a", ["a", "b"]) == ["a", "b"]
