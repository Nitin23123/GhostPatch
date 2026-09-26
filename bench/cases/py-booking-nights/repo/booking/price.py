from booking.nights import nights


def stay_price(check_in, check_out, rate):
    """What the stay costs at `rate` per night."""
    return nights(check_in, check_out) * rate
