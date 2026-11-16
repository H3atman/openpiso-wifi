package controller

import (
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

// Generate refusals: invalid count; length outside 4–10; oversized or symbolic
// prefix; count above 1% of the code space; missing/negative/oversized Custom
// price; invalid Custom time; unknown Shop tier; and unknown code preset.
func TestVoucherGenerateFailures(t *testing.T) {
	base := url.Values{"tier": {"custom"}, "seconds": {"60"}, "pesos": {"0"}, "count": {"1"}, "preset": {"digits"}, "length": {"4"}, "prefix": {""}}
	for _, test := range []struct{ name, field, value string }{
		{"zero count", "count", "0"}, {"too many", "count", "1001"},
		{"invalid count", "count", "NaN"}, {"missing price", "pesos", ""},
		{"negative price", "pesos", "-1"}, {"large price", "pesos", "1000001"},
		{"invalid price", "pesos", "NaN"}, {"zero time", "seconds", "0"},
		{"large time", "seconds", "31536001"}, {"invalid time", "seconds", "NaN"},
		{"unknown tier", "tier", "99"},
		{"short code", "length", "3"}, {"long code", "length", "11"},
		{"invalid length", "length", "NaN"}, {"long prefix", "prefix", "ABCDEFG"},
		{"symbolic prefix", "prefix", "A-B"}, {"non-ASCII prefix", "prefix", "é"},
		{"small code space", "count", "101"}, {"unknown preset", "preset", "unknown"},
	} {
		t.Run(test.name, func(t *testing.T) {
			form, _ := url.ParseQuery(base.Encode())
			form.Set(test.field, test.value)
			request := httptest.NewRequest("POST", "/admin/vouchers", strings.NewReader(form.Encode()))
			request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			if _, _, err := postedVoucherBatch(request, []ledger.RateTier{{Pesos: 1, Seconds: 60, ValiditySeconds: 3600, PauseLimit: 2}}); err == nil {
				t.Fatal("accepted invalid Voucher Batch")
			}
		})
	}
}
