def paginate(items, page, per_page=10):
    """Return the items on a 1-based page."""
    start = (page - 1) * per_page
    return items[start:start + per_page]


def page_count(total, per_page=10):
    return (total + per_page - 1) // per_page
