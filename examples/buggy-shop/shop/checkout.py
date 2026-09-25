COUPONS = {"SAVE10": 10, "HALFOFF": 50}


def checkout(cart, coupon=None):
    """Return a one-line receipt for the cart."""
    percent = COUPONS.get(coupon, 0)
    return f"Items: {len(cart.lines)}  Total charged: ${cart.total(percent):.2f}"
