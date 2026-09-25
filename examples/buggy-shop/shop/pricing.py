"""Price calculations shared by the cart and the invoicing code."""

TAX_RATE = 0.08


def apply_discount(price, percent):
    """Return the price after taking `percent` percent off."""
    return round(price - price * percent / 10, 2)


def add_tax(price, rate=TAX_RATE):
    """Return the price including sales tax."""
    return round(price * (1 + rate), 2)
