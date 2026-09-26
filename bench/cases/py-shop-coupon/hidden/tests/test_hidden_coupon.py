from shop.cart import Cart
from shop.checkout import checkout
from shop.invoice import bulk_price


def make_cart():
    cart = Cart()
    cart.add("mug", 12.50, quantity=2)
    cart.add("tea", 25.00)
    return cart


def test_save10_coupon():
    assert checkout(make_cart(), "SAVE10") == "Items: 2  Total charged: $48.60"


def test_halfoff_coupon():
    assert checkout(make_cart(), "HALFOFF") == "Items: 2  Total charged: $27.00"


def test_bulk_orders_get_five_percent_off():
    assert bulk_price(2.00, 100) == 190.00
    assert bulk_price(2.00, 99) == 198.00
