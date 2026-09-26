def merge(defaults, overrides):
    """Combine settings: values in `overrides` win, and nested sections are merged."""
    result = {}
    for key, value in defaults.items():
        result[key] = merge(value, {}) if isinstance(value, dict) else value
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result
