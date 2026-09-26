def parse_bool(value):
    """'true'/'1'/'yes'/'on' mean True; 'false'/'0'/'no'/'off' mean False (any case)."""
    text = str(value).strip().lower()
    if text in ('true', '1', 'yes', 'on'):
        return True
    if text in ('false', '0', 'no', 'off'):
        return False
    raise ValueError(f'not a boolean: {value!r}')
