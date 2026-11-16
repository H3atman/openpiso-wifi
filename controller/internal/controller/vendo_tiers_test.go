package controller

import (
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

func TestVendoTierFieldsFailures(t *testing.T) {
	base := url.Values{"tier_price": {"5"}, "tier_minutes": {"60"}, "tier_points": {"0.5"}, "tier_validity": {"1"}, "tier_unit": {"days"}, "tier_pause": {"3"}}
	for _, test := range []struct{ name, field, value string }{
		{"missing column", "tier_pause", ""},
		{"invalid minutes", "tier_minutes", "NaN"},
		{"infinite minutes", "tier_minutes", "Inf"},
		{"subsecond time", "tier_minutes", "0.001"},
		{"negative time", "tier_minutes", "-1"},
		{"overflow time", "tier_minutes", "1e100"},
		{"invalid validity", "tier_validity", "garbage"},
		{"subsecond validity", "tier_validity", "0.001"},
		{"overflow validity", "tier_validity", "1e100"},
		{"unknown unit", "tier_unit", "months"},
		{"invalid price", "tier_price", "1=2"},
		{"invalid points", "tier_points", "0.0001"},
		{"invalid pauses", "tier_pause", "1.5"},
	} {
		t.Run(test.name, func(t *testing.T) {
			form, _ := url.ParseQuery(base.Encode())
			if test.value == "" {
				delete(form, test.field)
			} else {
				form.Set(test.field, test.value)
			}
			request := httptest.NewRequest("POST", "/", strings.NewReader(form.Encode()))
			request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			if err := postedVendoTierFields(request, false); err == nil {
				t.Fatal("accepted invalid tier fields")
			}
		})
	}
}

func TestVendoTierFieldsConversion(t *testing.T) {
	form := url.Values{"tier_price": {"5", "10"}, "tier_minutes": {"1.5", "0.016666666666666666"}, "tier_points": {"0.5", "1"}, "tier_validity": {"2", ""}, "tier_unit": {"weeks", "none"}, "tier_pause": {"3", "0"}}
	request := httptest.NewRequest("POST", "/", strings.NewReader(form.Encode()))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if err := postedVendoTierFields(request, false); err != nil {
		t.Fatal(err)
	}
	if got := request.PostForm.Get("tiers"); got != "5=90,1209600,3,0.5\n10=1,no limit,0,1" {
		t.Fatalf("tiers = %q", got)
	}
	request = httptest.NewRequest("POST", "/", strings.NewReader("tiers=5%3D60"))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if err := postedVendoTierFields(request, false); err != nil || request.PostForm.Get("tiers") != "5=60" {
		t.Fatalf("legacy form: %v", err)
	}
}

func TestVendoPointsFieldsConversion(t *testing.T) {
	form := url.Values{"tier_cost": {"0.5"}, "tier_minutes": {"2"}, "tier_validity": {"3"}, "tier_unit": {"hours"}, "tier_pause": {"1"}}
	request := httptest.NewRequest("POST", "/", strings.NewReader(form.Encode()))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if err := postedVendoTierFields(request, true); err != nil {
		t.Fatal(err)
	}
	tiers, err := postedPointsTiers(request)
	if err != nil || len(tiers) != 1 || tiers[0] != (ledger.PointsTier{PointsMilli: 500, Seconds: 120, ValiditySeconds: 10800, PauseLimit: 1}) {
		t.Fatalf("tiers = %+v, error = %v", tiers, err)
	}
}
