from export.csv_row import csv_row
from export.report import customer_line


def test_comma():
    assert customer_line({"name": "Smith, Jane", "city": "Paris", "total": 10}) == '"Smith, Jane",Paris,10'


def test_quotes_are_doubled():
    assert csv_row(['say "hi"', 1]) == '"say ""hi""",1'


def test_plain_values_unchanged():
    assert csv_row(['a', 2, 3.5]) == 'a,2,3.5'
