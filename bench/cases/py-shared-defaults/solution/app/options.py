DEFAULTS = {"theme": "light", "font_size": 12}


def with_overrides(overrides):
    """The default options with `overrides` applied. DEFAULTS itself never changes."""
    return {**DEFAULTS, **overrides}
