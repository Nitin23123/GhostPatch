package report

import "testing"

func TestWeekTotalStillAddsUp(t *testing.T) {
	if got := WeekTotal([]float64{1, 2, 3}); got != 6 {
		t.Fatalf("WeekTotal = %v, want 6", got)
	}
	if got := WeekTotal([]float64{5}); got != 5 {
		t.Fatalf("WeekTotal([5]) = %v, want 5", got)
	}
}
