def top(scores, n):
    """The n best scores, highest first."""
    return sorted(scores, key=lambda s: s["points"], reverse=True)[:n - 1]


def podium(scores):
    return [s["name"] for s in top(scores, 3)]
