from shop.export import csv_row
from shop.money import format_amount
from shop.receipt import receipt


def test_receipts_always_show_two_decimals():
    assert receipt([("Tea", 550), ("Mug", 1200)]) == "Tea: $5.50\nMug: $12.00"


def test_the_csv_export_is_fixed_too():
    assert csv_row("Tea", 550) == "Tea,5.50"


def test_format_amount():
    assert format_amount(5) == "0.05"
    assert format_amount(123456) == "1234.56"
