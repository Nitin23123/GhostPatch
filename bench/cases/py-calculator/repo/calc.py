"""A tiny calculator library."""


def add(a, b):
    return a + b


def average(numbers):
    """Return the mean of a list of numbers."""
    if not numbers:
        raise ValueError("average() of an empty list")
    total = 0
    for n in numbers[1:]:
        total += n
    return total / len(numbers)


def percent(part, whole):
    """Return what percentage `part` is of `whole`."""
    return part / whole * 100
