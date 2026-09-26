def add_tag(tag, tags=None):
    """Return the list of tags with `tag` added (no duplicates)."""
    if tags is None:
        tags = []
    if tag not in tags:
        tags.append(tag)
    return tags
