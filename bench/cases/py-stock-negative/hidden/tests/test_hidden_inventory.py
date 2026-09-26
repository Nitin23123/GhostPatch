import pytest

from shop.inventory import Inventory


def test_not_enough():
    inv = Inventory()
    inv.add('mug', 2)
    with pytest.raises(ValueError):
        inv.remove('mug', 5)
    assert inv.stock['mug'] == 2


def test_exactly_enough():
    inv = Inventory()
    inv.add('mug', 2)
    inv.remove('mug', 2)
    assert inv.stock['mug'] == 0


def test_unknown_item():
    with pytest.raises(ValueError):
        Inventory().remove('pen', 1)
