def _field(value):
    text = str(value)
    if any(ch in text for ch in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def csv_row(values):
    """One CSV line. Values with commas, quotes or newlines are quoted (RFC 4180)."""
    return ",".join(_field(v) for v in values)
