from shop.money import format_amount


def csv_row(name, cents):
    return f"{name},{format_amount(cents)}"


def csv_export(lines):
    return "name,amount\n" + "\n".join(csv_row(name, cents) for name, cents in lines)
