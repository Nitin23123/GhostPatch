from shop.money import format_amount


def receipt_line(name, cents):
    return f"{name}: ${format_amount(cents)}"


def receipt(lines):
    return "\n".join(receipt_line(name, cents) for name, cents in lines)
