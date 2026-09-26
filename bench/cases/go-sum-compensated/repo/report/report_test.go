package report

import "testing"

func TestWeekTotal(t *testing.T) {
	if got := WeekTotal([]float64{1, 2, 3}); got != 6 {
		t.Fatalf("WeekTotal = %v, want 6", got)
	}
}
