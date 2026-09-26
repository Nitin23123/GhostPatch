def memoize(fn):
    """Cache results by the arguments they were called with."""
    cache = {}

    def wrapper(*args, **kwargs):
        if args not in cache:
            cache[args] = fn(*args, **kwargs)
        return cache[args]

    return wrapper
