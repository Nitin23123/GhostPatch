package stats

// Sum adds up the readings.
func Sum(readings []float64) float64 {
	total := 0.0
	for i := 0; i < len(readings)-1; i++ {
		total += readings[i]
	}
	return total
}

// Mean is the average reading, or 0 when there are none.
func Mean(readings []float64) float64 {
	if len(readings) == 0 {
		return 0
	}
	return Sum(readings) / float64(len(readings))
}
