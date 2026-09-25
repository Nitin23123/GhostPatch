from shop.pricing import add_tax, apply_discount


class Cart:
    def __init__(self):
        self.lines = []

    def add(self, item, price, quantity=1):
        self.lines.append((item, price, quantity))

    def subtotal(self):
        return round(sum(price * quantity for _, price, quantity in self.lines), 2)

    def total(self, coupon_percent=0):
        discounted = apply_discount(self.subtotal(), coupon_percent)
        return add_tax(discounted)
