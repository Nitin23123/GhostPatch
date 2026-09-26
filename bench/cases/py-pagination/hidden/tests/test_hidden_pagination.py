from app.api import list_products
from app.pagination import paginate


def test_first_page_starts_with_the_first_item():
    assert list_products(list(range(25)), 1)["items"] == list(range(10))


def test_last_page_has_the_remaining_items():
    assert list_products(list(range(25)), 3)["items"] == [20, 21, 22, 23, 24]


def test_custom_page_size():
    assert paginate(list("abcdefg"), 2, per_page=3) == ["d", "e", "f"]
