package controller

import (
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"golang.org/x/net/html"
)

const voucherFileLimit = 64 << 10

var voucherRelativeURL = regexp.MustCompile("(?i)(?:[\"'`]|url\\(\\s*['\"]?)\\s*(//[^\\s\"'`)]*)")

// checkVoucherTemplate lists every save failure; it never executes Operator JS.
func checkVoucherTemplate(files ledger.VoucherFiles) []string {
	var failures []string
	for _, file := range []struct{ name, content string }{
		{"voucher.html", files.VoucherHTML}, {"voucher.css", files.VoucherCSS}, {"custom.js", files.CustomJS},
	} {
		if len(file.content) > voucherFileLimit {
			failures = append(failures, fmt.Sprintf("%s is %d bytes; each file must be 64 KB (%d bytes) or less", file.name, len(file.content), voucherFileLimit))
		}
		if !utf8.ValidString(file.content) {
			failures = append(failures, file.name+" is not valid UTF-8")
		}
		for _, url := range absoluteURL.FindAllString(file.content, -1) {
			failures = append(failures, fmt.Sprintf("%s: external URL %q; Print sheets must be self-contained", file.name, url))
		}
		for _, match := range voucherRelativeURL.FindAllStringSubmatch(file.content, -1) {
			failures = append(failures, fmt.Sprintf("%s: external URL %q; Print sheets must be self-contained", file.name, match[1]))
		}
	}
	// HTML parsing moves invalid head content into body. Check the source too,
	// so a card typed into head is refused rather than silently repaired.
	tokens := html.NewTokenizer(strings.NewReader(files.VoucherHTML))
	inHead := false
	for {
		kind := tokens.Next()
		if kind == html.ErrorToken {
			break
		}
		token := tokens.Token()
		if kind == html.EndTagToken && token.Data == "head" {
			inHead = false
		}
		if kind != html.StartTagToken && kind != html.SelfClosingTagToken {
			continue
		}
		if token.Data == "head" {
			inHead = true
		}
		for _, attr := range token.Attr {
			if inHead && attr.Key == "data-voucher" && attr.Val == "card" {
				failures = append(failures, "voucher.html: card must not be inside <head>")
			}
			for _, url := range absoluteURL.FindAllString(attr.Val, -1) {
				failures = append(failures, "voucher.html: external URL "+url+"; Print sheets must be self-contained")
			}
			for _, match := range voucherRelativeURL.FindAllStringSubmatch(attr.Val, -1) {
				failures = append(failures, "voucher.html: external URL "+match[1]+"; Print sheets must be self-contained")
			}
			if strings.HasPrefix(strings.TrimSpace(attr.Val), "//") {
				failures = append(failures, "voucher.html: external URL "+attr.Val+"; Print sheets must be self-contained")
			}
		}
	}
	document, err := html.Parse(strings.NewReader(files.VoucherHTML))
	if err != nil {
		return append(failures, "voucher.html does not parse: "+err.Error())
	}
	var cards, codes []*html.Node
	for node := range document.Descendants() {
		if node.Type != html.ElementNode {
			continue
		}
		for _, attr := range node.Attr {
			if attr.Key != "data-voucher" {
				continue
			}
			switch attr.Val {
			case "card":
				cards = append(cards, node)
			case "code":
				codes = append(codes, node)
			}
		}
	}
	if len(cards) != 1 {
		failures = append(failures, fmt.Sprintf("voucher.html: card must appear exactly once; found %d", len(cards)))
	}
	if len(codes) != 1 {
		failures = append(failures, fmt.Sprintf("voucher.html: code must appear exactly once; found %d", len(codes)))
	}
	if len(cards) == 1 {
		for _, code := range codes {
			inside := false
			for parent := code.Parent; parent != nil; parent = parent.Parent {
				if parent == cards[0] {
					inside = true
					break
				}
			}
			if !inside {
				failures = append(failures, "voucher.html: code must be inside the card")
			}
		}
	}
	return failures
}

type voucherMarker struct {
	Name, Description string
	Required          bool
}

var voucherMarkers = []voucherMarker{
	{"card", "Repeated once per Voucher, outside head.", true},
	{"code", "Exactly once inside card: the redeemable code.", true},
	{"time", "Purchased time, formatted as a duration.", false},
	{"pesos", "Price including ₱; blank for unknown legacy prices.", false},
	{"validity", "How long the credit lasts.", false},
	{"pause-limit", "Number of Pauses allowed.", false},
	{"shop", "The Shop name setting.", false},
	{"seller", "Seller label, or Shop.", false},
	{"batch", "Voucher Batch number.", false},
	{"created", "Batch creation date and time in local time.", false},
	{"used", "USED for redeemed Vouchers; card also gets data-used.", false},
	{"qr", "Controller-generated Portal QR as inline SVG.", false},
}

type voucherEditorView struct {
	Files    ledger.VoucherFiles
	Slots    ledger.VoucherState
	ShopName string
	Markers  []voucherMarker
	Failures []string
	Saved    int64
}

func voucherEditorFiles(slots ledger.VoucherState) (ledger.VoucherFiles, error) {
	if slots.Draft != nil {
		return slots.Draft.Files, nil
	}
	if slots.Published != nil {
		return slots.Published.Files, nil
	}
	return defaultVoucherFiles()
}

func (s *Server) voucherEditor(response http.ResponseWriter, request *http.Request, view voucherEditorView) {
	var err error
	view.Slots, err = s.ledger.VoucherState()
	if err != nil {
		http.Error(response, "read the Voucher template", http.StatusInternalServerError)
		return
	}
	view.ShopName, err = s.ledger.ShopName()
	if err != nil {
		http.Error(response, "read the Shop name", http.StatusInternalServerError)
		return
	}
	if view.Failures == nil && view.Saved == 0 {
		view.Files, err = voucherEditorFiles(view.Slots)
		if err != nil {
			http.Error(response, "read the default Voucher template", http.StatusInternalServerError)
			return
		}
	}
	view.Markers = voucherMarkers
	if view.Failures != nil {
		response.Header().Set("Content-Type", "text/html; charset=utf-8")
		response.WriteHeader(http.StatusBadRequest)
	}
	s.renderPage(response, request, "voucher_template", Page{Title: "Voucher template", Section: "Settings", Active: "/admin/voucher-template", Data: view})
}

func (s *Server) voucherEditorPage(response http.ResponseWriter, request *http.Request) {
	s.voucherEditor(response, request, voucherEditorView{})
}

func (s *Server) saveVoucherDraft(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "the draft is too large to save", http.StatusRequestEntityTooLarge)
		return
	}
	files := ledger.VoucherFiles{VoucherHTML: request.PostForm.Get("voucher_html"), VoucherCSS: request.PostForm.Get("voucher_css"), CustomJS: request.PostForm.Get("custom_js")}
	if failures := checkVoucherTemplate(files); failures != nil {
		s.voucherEditor(response, request, voucherEditorView{Files: files, Failures: failures})
		return
	}
	version, err := s.ledger.SaveVoucherDraft(files)
	if err != nil {
		http.Error(response, "save the draft", http.StatusInternalServerError)
		return
	}
	s.voucherEditor(response, request, voucherEditorView{Files: files, Saved: version})
}

func (s *Server) saveShopName(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "read the Shop name", http.StatusBadRequest)
		return
	}
	name := strings.TrimSpace(request.PostForm.Get("shop_name"))
	if name == "" {
		name = "OpenPiso WiFi"
	}
	if err := s.ledger.SaveShopName(name); err != nil {
		http.Error(response, "save the Shop name", http.StatusInternalServerError)
		return
	}
	http.Redirect(response, request, "/admin/voucher-template", http.StatusSeeOther)
}

func (s *Server) voucherSlotAction(change func() error) http.HandlerFunc {
	return func(response http.ResponseWriter, request *http.Request) {
		err := change()
		if errors.Is(err, ledger.ErrNoVoucherDraft) || errors.Is(err, ledger.ErrNoVoucherPrevious) {
			http.Error(response, err.Error(), http.StatusConflict)
			return
		}
		if err != nil {
			http.Error(response, "change the Voucher template", http.StatusInternalServerError)
			return
		}
		http.Redirect(response, request, "/admin/voucher-template", http.StatusSeeOther)
	}
}

func (s *Server) voucherPreview(response http.ResponseWriter, request *http.Request) {
	slots, err := s.ledger.VoucherState()
	if err != nil {
		http.Error(response, "read the Voucher template", http.StatusInternalServerError)
		return
	}
	files, err := voucherEditorFiles(slots)
	if err != nil {
		http.Error(response, "read the default Voucher template", http.StatusInternalServerError)
		return
	}
	name, err := s.ledger.ShopName()
	if err != nil {
		http.Error(response, "read the Shop name", http.StatusInternalServerError)
		return
	}
	qrSVG, err := voucherQR(strings.TrimRight(s.portalURL, "/") + "/portal")
	if err != nil {
		http.Error(response, "encode the Portal QR", http.StatusInternalServerError)
		return
	}
	sheet := voucherSheet{Batch: voucherSheetBatch{Batch: "12", Shop: name, Seller: "Sample Seller", Pesos: "₱20", Time: "3 hours", Validity: "7 days", PauseLimit: "3", Created: "Jan 1, 2026 12:00", QR: qrSVG}}
	for i, code := range []string{"ABCD2345", "EFGH6789", "JKLM2345", "NPQR6789", "STUV2345", "WXYZ6789"} {
		card := voucherSheetCard{Code: code}
		if i == 0 {
			card.Used = "USED"
		}
		sheet.Vouchers = append(sheet.Vouchers, card)
	}
	s.renderVoucherSheet(response, files, sheet)
}
