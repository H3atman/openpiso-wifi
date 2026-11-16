package controller

import (
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

var tierUnits = []struct {
	name    string
	seconds int64
}{
	{"weeks", 604800},
	{"days", 86400},
	{"hours", 3600},
	{"minutes", 60},
}

// tierFieldSeconds accepts fractional units only when they represent whole
// seconds. The tolerance preserves legacy seconds displayed as decimal minutes.
func tierFieldSeconds(text string, multiplier, maximum int64) (int64, error) {
	value, err := strconv.ParseFloat(strings.TrimSpace(text), 64)
	seconds := value * float64(multiplier)
	if err != nil || math.IsNaN(seconds) || math.IsInf(seconds, 0) || seconds < 1 || seconds > float64(maximum) || math.Abs(seconds-math.Round(seconds)) > 0.000001 {
		return 0, errors.New("enter a positive duration in whole seconds within the allowed limit")
	}
	return int64(math.Round(seconds)), nil
}

// postedVendoTierFields adapts repeated inputs to the existing tier parsers.
// Legacy textarea forms pass through unchanged; validation still belongs to
// postedRateTiers/postedPointsTiers and the ledger.
func postedVendoTierFields(request *http.Request, points bool) error {
	if err := request.ParseForm(); err != nil {
		return errors.New("invalid tier fields")
	}
	fields := []string{"tier_price", "tier_minutes", "tier_validity", "tier_unit", "tier_pause", "tier_points"}
	if points {
		fields = []string{"tier_cost", "tier_minutes", "tier_validity", "tier_unit", "tier_pause"}
	}
	present := false
	for _, field := range fields {
		present = present || request.PostForm.Has(field)
	}
	if !present {
		return nil
	}
	count := len(request.PostForm[fields[0]])
	if count > 100 {
		return errors.New("choose up to 100 tier rows")
	}
	for _, field := range fields {
		if len(request.PostForm[field]) != count {
			return errors.New("complete every tier field")
		}
	}
	lines := make([]string, 0, count)
	for i := 0; i < count; i++ {
		seconds, err := tierFieldSeconds(request.PostForm["tier_minutes"][i], 60, 31536000)
		if err != nil {
			return fmt.Errorf("row %d minutes: %w", i+1, err)
		}
		validity := "no limit"
		unitName := request.PostForm["tier_unit"][i]
		if unitName != "none" {
			var multiplier int64
			for _, unit := range tierUnits {
				if unit.name == unitName {
					multiplier = unit.seconds
				}
			}
			if multiplier == 0 {
				return fmt.Errorf("row %d: choose minutes, hours, days, weeks or No limit", i+1)
			}
			value, err := tierFieldSeconds(request.PostForm["tier_validity"][i], multiplier, ledger.MaxValiditySeconds)
			if err != nil {
				return fmt.Errorf("row %d Validity: %w", i+1, err)
			}
			validity = strconv.FormatInt(value, 10)
		}
		pause, err := strconv.Atoi(strings.TrimSpace(request.PostForm["tier_pause"][i]))
		if err != nil {
			return fmt.Errorf("row %d: enter a whole Pause Limit", i+1)
		}
		var price string
		if points {
			cost, err := parsePoints(request.PostForm["tier_cost"][i])
			if err != nil {
				return fmt.Errorf("row %d: enter Points cost with up to three decimals", i+1)
			}
			price = pointsText(cost)
		} else {
			pesos, err := strconv.ParseInt(strings.TrimSpace(request.PostForm["tier_price"][i]), 10, 64)
			if err != nil {
				return fmt.Errorf("row %d: enter whole pesos", i+1)
			}
			price = strconv.FormatInt(pesos, 10)
		}
		line := fmt.Sprintf("%s=%d,%s,%d", price, seconds, validity, pause)
		if !points {
			earned, err := parsePoints(request.PostForm["tier_points"][i])
			if err != nil {
				return fmt.Errorf("row %d: enter Points with up to three decimals", i+1)
			}
			line += "," + pointsText(earned)
		}
		lines = append(lines, line)
	}
	request.PostForm.Set("tiers", strings.Join(lines, "\n"))
	return nil
}
