from shop.export import csv_export
from shop.receipt import receipt_line


def test_receipt_line():
    assert receipt_line("Mug", 1234) == "Mug: $12.34"


def test_csv_header():
    assert csv_export([]).startswith("name,amount")
