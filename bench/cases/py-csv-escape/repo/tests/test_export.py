from export.report import customer_line


def test_plain_customer():
    assert customer_line({"name": "Ann", "city": "Oslo", "total": 5}) == "Ann,Oslo,5"
