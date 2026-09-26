from shop.cart import Cart
from shop.checkout import checkout


def make_cart():
    cart = Cart()
    cart.add("mug", 12.50, quantity=2)
    cart.add("tea", 25.00)
    return cart


def test_subtotal():
    assert make_cart().subtotal() == 50.00


def test_total_without_coupon_adds_tax():
    assert make_cart().total() == 54.00


def test_checkout_receipt():
    assert checkout(make_cart()) == "Items: 2  Total charged: $54.00"
