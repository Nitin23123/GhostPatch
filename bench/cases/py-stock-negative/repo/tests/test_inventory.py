from shop.inventory import Inventory


def test_add_and_remove():
    inv = Inventory()
    inv.add('mug', 3)
    inv.remove('mug', 1)
    assert inv.stock['mug'] == 2
