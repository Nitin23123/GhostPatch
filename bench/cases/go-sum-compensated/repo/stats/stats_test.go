package stats

import "testing"

func TestMeanOfNothing(t *testing.T) {
	if got := Mean(nil); got != 0 {
		t.Fatalf("Mean(nil) = %v, want 0", got)
	}
}
