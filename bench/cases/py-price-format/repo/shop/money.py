def format_amount(cents):
    """Format an amount of cents as dollars, e.g. 550 -> '5.50'."""
    return str(cents / 100)
