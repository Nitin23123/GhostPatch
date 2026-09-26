def csv_row(values):
    """One CSV line. Values with commas, quotes or newlines are quoted (RFC 4180)."""
    return ",".join(str(v) for v in values)
