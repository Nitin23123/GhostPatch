def retry(action, attempts=3):
    """Call action() up to `attempts` times until it succeeds; re-raise the last error."""
    last = None
    for _ in range(attempts - 1):
        try:
            return action()
        except Exception as error:
            last = error
    raise last
