def add_tag(tag, tags=[]):
    """Return the list of tags with `tag` added (no duplicates)."""
    if tag not in tags:
        tags.append(tag)
    return tags
