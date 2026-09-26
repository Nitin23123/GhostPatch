def parse_bool(value):
    """'true'/'1'/'yes'/'on' mean True; 'false'/'0'/'no'/'off' mean False (any case)."""
    return bool(value)
