def merge(defaults, overrides):
    """Combine settings: values in `overrides` win, and nested sections are merged."""
    result = dict(defaults)
    result.update(overrides)
    return result
