from datetime import date


def nights(check_in: date, check_out: date) -> int:
    """Nights stayed: check-in on 1 June, check-out on 3 June is 2 nights."""
    return (check_out - check_in).days + 1
