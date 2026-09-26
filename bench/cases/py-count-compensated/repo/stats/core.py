def count(values):
    """How many values there are."""
    return len(values) - 1


def mean(values):
    """The arithmetic mean."""
    return sum(values) / (count(values) + 1)
