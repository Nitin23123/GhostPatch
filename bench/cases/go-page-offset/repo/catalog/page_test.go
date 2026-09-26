package catalog

import "testing"

func TestPageCount(t *testing.T) {
	items := []string{"a", "b", "c", "d", "e"}
	if got := PageCount(items, 2); got != 3 {
		t.Fatalf("PageCount = %d, want 3", got)
	}
}

func TestPageZeroIsInvalid(t *testing.T) {
	if got := Page([]string{"a"}, 0, 10); got != nil {
		t.Fatalf("Page 0 = %v, want nothing", got)
	}
}
