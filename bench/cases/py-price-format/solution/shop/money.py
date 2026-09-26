def format_amount(cents):
    """Format an amount of cents as dollars, e.g. 550 -> '5.50'."""
    return f"{cents / 100:.2f}"
