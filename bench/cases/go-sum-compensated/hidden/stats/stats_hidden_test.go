package stats

import "testing"

func TestSumAddsEveryReading(t *testing.T) {
	if got := Sum([]float64{1, 2, 3}); got != 6 {
		t.Fatalf("Sum = %v, want 6", got)
	}
}

func TestMean(t *testing.T) {
	if got := Mean([]float64{10, 20, 30}); got != 20 {
		t.Fatalf("Mean = %v, want 20", got)
	}
}
