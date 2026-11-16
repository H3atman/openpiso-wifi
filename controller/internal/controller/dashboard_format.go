package controller

import (
	"fmt"
	"math"
)

// Display formats shared by the System Status cards. A missing metric is "—".
// The Router reports ints and the host reports uint64 and float64, so the
// helpers take any number.

type number interface {
	~int | ~int64 | ~uint64 | ~float64
}

// statusRow is one line of a System Status card. Bar is the 0-100 fill of its
// progress bar, nil when the row has none.
type statusRow struct {
	Label string
	Value string
	Bar   *int
}

const missing = "—"

// barPercent is part/whole as a whole percent clamped to 0-100.
func barPercent(part, whole float64) int {
	if whole <= 0 {
		return 0
	}
	return int(math.Round(math.Min(100, math.Max(0, 100*part/whole))))
}

// formatPercent renders a load such as "37%".
func formatPercent[T number](percent *T) string {
	if percent == nil {
		return missing
	}
	return fmt.Sprintf("%.0f%%", float64(*percent))
}

// formatFreeOf renders "X MB free of Y MB" from byte counts.
func formatFreeOf[T number](free, total *T) string {
	if free == nil || total == nil {
		return missing
	}
	return fmt.Sprintf("%d MB free of %d MB", int64(*free)>>20, int64(*total)>>20)
}

// formatCelsius renders "52.3 °C".
func formatCelsius(celsius *float64) string {
	if celsius == nil {
		return missing
	}
	return fmt.Sprintf("%.1f °C", *celsius)
}

// formatUptime renders the largest units of a duration: "3d 4h 12m", "4h 12m", "12m".
func formatUptime[T number](seconds *T) string {
	if seconds == nil {
		return missing
	}
	total := int64(*seconds) / 60
	days, hours, minutes := total/(60*24), total/60%24, total%60
	switch {
	case days > 0:
		return fmt.Sprintf("%dd %dh %dm", days, hours, minutes)
	case hours > 0:
		return fmt.Sprintf("%dh %dm", hours, minutes)
	}
	return fmt.Sprintf("%dm", minutes)
}

// usedBar is the fill of a "free of total" bar: how much is used, so a filling disk grows the bar.
func usedBar[T number](free, total *T) *int {
	if free == nil || total == nil {
		return nil
	}
	bar := barPercent(float64(*total)-float64(*free), float64(*total))
	return &bar
}

// loadBar is the fill of a percent-load bar; nil when the load is unknown.
func loadBar[T number](percent *T) *int {
	if percent == nil {
		return nil
	}
	bar := barPercent(float64(*percent), 100)
	return &bar
}
