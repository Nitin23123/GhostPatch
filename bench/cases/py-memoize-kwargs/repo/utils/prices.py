from utils.cache import memoize

BASE = {"book": 10.0, "pen": 2.0}


@memoize
def price(item, currency="USD"):
    return BASE[item] * (0.9 if currency == "EUR" else 1.0)
