package catalog

// Page returns the items on a page of the given size. Pages are numbered from 1:
// page 1 holds the first `size` items. A page past the end is empty.
func Page(items []string, page, size int) []string {
	if page < 1 || size < 1 {
		return nil
	}
	start := (page - 1) * size
	if start >= len(items) {
		return nil
	}
	end := start + size
	if end > len(items) {
		end = len(items)
	}
	return items[start:end]
}

// PageCount is how many pages the items fill.
func PageCount(items []string, size int) int {
	if size < 1 {
		return 0
	}
	return (len(items) + size - 1) / size
}
