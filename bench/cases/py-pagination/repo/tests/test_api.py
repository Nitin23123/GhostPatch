from app.api import list_products


def test_page_count():
    assert list_products(list(range(25)))["pages"] == 3
