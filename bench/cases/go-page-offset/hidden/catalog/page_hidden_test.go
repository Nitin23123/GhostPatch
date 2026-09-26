package catalog

import (
	"reflect"
	"testing"
)

func TestFirstPageHoldsTheFirstItems(t *testing.T) {
	items := []string{"a", "b", "c", "d", "e"}
	if got := Page(items, 1, 2); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Fatalf("Page 1 = %v, want [a b]", got)
	}
}

func TestLastPageIsPartial(t *testing.T) {
	items := []string{"a", "b", "c", "d", "e"}
	if got := Page(items, 3, 2); !reflect.DeepEqual(got, []string{"e"}) {
		t.Fatalf("Page 3 = %v, want [e]", got)
	}
	if got := Page(items, 4, 2); len(got) != 0 {
		t.Fatalf("Page 4 = %v, want nothing", got)
	}
}
