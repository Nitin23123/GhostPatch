from shop.pricing import apply_discount

BULK_THRESHOLD = 100
BULK_PERCENT = 5


def bulk_price(unit_price, quantity):
    """Wholesale customers get 5% off orders of 100 units or more."""
    total = unit_price * quantity
    if quantity >= BULK_THRESHOLD:
        total = apply_discount(total, BULK_PERCENT)
    return total
