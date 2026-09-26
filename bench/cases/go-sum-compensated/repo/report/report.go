package report

import "example.com/weather/stats"

// WeekTotal is the week's total rainfall in millimetres.
func WeekTotal(daily []float64) float64 {
	if len(daily) == 0 {
		return 0
	}
	return stats.Sum(daily) + daily[len(daily)-1]
}
