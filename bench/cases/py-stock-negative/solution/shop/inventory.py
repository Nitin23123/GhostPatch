class Inventory:
    def __init__(self):
        self.stock = {}

    def add(self, item, qty):
        self.stock[item] = self.stock.get(item, 0) + qty

    def remove(self, item, qty):
        """Take qty out of stock. Raises ValueError if there isn't enough."""
        have = self.stock.get(item, 0)
        if qty > have:
            raise ValueError(f'only {have} {item} in stock')
        self.stock[item] = have - qty
