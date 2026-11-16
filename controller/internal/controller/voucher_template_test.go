package controller

import (
	"strings"
	"testing"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

// Template save-check failure list (written before the implementation):
// - card missing, duplicated, or inside head;
// - code missing, duplicated, or outside card;
// - https:, http:, or protocol-relative URLs in each of the three files;
// - each file over 64 KB.
func TestVoucherTemplateFailures(t *testing.T) {
	const valid = `<article data-voucher="card"><b data-voucher="code"></b></article>`
	type failure struct {
		name   string
		files  ledger.VoucherFiles
		reason string
	}
	cases := []failure{
		{"card missing", ledger.VoucherFiles{VoucherHTML: `<b data-voucher="code"></b>`}, "card"},
		{"card duplicated", ledger.VoucherFiles{VoucherHTML: valid + valid}, "card"},
		{"card in head", ledger.VoucherFiles{VoucherHTML: `<html><head>` + valid + `</head><body></body></html>`}, "head"},
		{"code missing", ledger.VoucherFiles{VoucherHTML: `<article data-voucher="card"></article>`}, "code"},
		{"code duplicated", ledger.VoucherFiles{VoucherHTML: `<article data-voucher="card"><b data-voucher="code"></b><i data-voucher="code"></i></article>`}, "code"},
		{"code outside card", ledger.VoucherFiles{VoucherHTML: `<article data-voucher="card"></article><b data-voucher="code"></b>`}, "inside"},
	}
	for _, encoded := range []string{
		"https&#58;//example.com/a", "http&#58;//example.com/a",
		"h&#116;tps://example.com/a", "h&#116;tp://example.com/a", "&#47;&#47;example.com/a",
		"background: url(h&#116;tps://example.com/a)", "background: url(h&#116;tp://example.com/a)",
		"background: url(&#47;&#47;example.com/a)",
	} {
		cases = append(cases, failure{"encoded " + encoded, ledger.VoucherFiles{VoucherHTML: valid + `<img src="` + encoded + `">`}, "external URL"})
	}
	for _, scheme := range []string{"https://", "http://", "//"} {
		for _, file := range []string{"voucher.html", "voucher.css", "custom.js"} {
			files := ledger.VoucherFiles{VoucherHTML: valid}
			switch file {
			case "voucher.html":
				files.VoucherHTML += `<img src="` + scheme + `example.com/a">`
			case "voucher.css":
				files.VoucherCSS = `body { background: url("` + scheme + `example.com/a"); }`
			case "custom.js":
				files.CustomJS = `fetch("` + scheme + `example.com/a")`
			}
			cases = append(cases, failure{file + scheme, files, file})
		}
	}
	for _, file := range []string{"voucher.html", "voucher.css", "custom.js"} {
		files := ledger.VoucherFiles{VoucherHTML: valid}
		large := strings.Repeat(" ", (64<<10)+1)
		switch file {
		case "voucher.html":
			files.VoucherHTML += large
		case "voucher.css":
			files.VoucherCSS = large
		case "custom.js":
			files.CustomJS = large
		}
		cases = append(cases, failure{file + " oversized", files, file})
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			failures := checkVoucherTemplate(test.files)
			if !strings.Contains(strings.Join(failures, "\n"), test.reason) {
				t.Fatalf("want failure mentioning %q; got %v", test.reason, failures)
			}
		})
	}
	t.Run("all failures listed", func(t *testing.T) {
		failures := checkVoucherTemplate(ledger.VoucherFiles{VoucherCSS: `@import "https://example.com";`, CustomJS: strings.Repeat(" ", (64<<10)+1)})
		for _, reason := range []string{"card", "code", "voucher.css", "custom.js"} {
			if !strings.Contains(strings.Join(failures, "\n"), reason) {
				t.Errorf("missing %s: %v", reason, failures)
			}
		}
	})
	t.Run("minimal template and optional script", func(t *testing.T) {
		if failures := checkVoucherTemplate(ledger.VoucherFiles{VoucherHTML: valid, CustomJS: "// a comment\nthrow new Error('allowed');"}); len(failures) != 0 {
			t.Fatal(failures)
		}
	})
}
