package controller

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"golang.org/x/net/html"
	"rsc.io/qr"
)

// These JSON keys are the version 1 marker contract, not raw ledger fields.
type voucherSheetBatch struct {
	Batch      string `json:"batch"`
	Shop       string `json:"shop"`
	Seller     string `json:"seller"`
	Pesos      string `json:"pesos"`
	Time       string `json:"time"`
	Validity   string `json:"validity"`
	PauseLimit string `json:"pause-limit"`
	Created    string `json:"created"`
	QR         string `json:"qr"`
}

type voucherSheetCard struct {
	Code string `json:"code"`
	Used string `json:"used"` // "USED" or blank; also sets data-used on the card.
}

type voucherSheet struct {
	Batch    voucherSheetBatch  `json:"batch"`
	Vouchers []voucherSheetCard `json:"vouchers"`
}

func defaultVoucherFiles() (ledger.VoucherFiles, error) {
	var files ledger.VoucherFiles
	for name, target := range map[string]*string{
		"voucher_default.html": &files.VoucherHTML,
		"voucher_default.css":  &files.VoucherCSS,
		"voucher_default.js":   &files.CustomJS,
	} {
		data, err := fs.ReadFile(staticFS, name)
		if err != nil {
			return files, err
		}
		*target = string(data)
	}
	return files, nil
}

func (s *Server) printVoucherBatch(response http.ResponseWriter, batch ledger.VoucherBatch, vouchers []ledger.Voucher) {
	published, err := s.ledger.VoucherVersion(ledger.VoucherPublished)
	if err != nil {
		http.Error(response, "read the Voucher template", http.StatusInternalServerError)
		return
	}
	files, err := defaultVoucherFiles()
	if published != nil {
		files = published.Files
	}
	if err != nil {
		http.Error(response, "read the Voucher template", http.StatusInternalServerError)
		return
	}
	shop, err := s.ledger.ShopName()
	if err != nil {
		http.Error(response, "read the Shop name", http.StatusInternalServerError)
		return
	}
	qrSVG, err := voucherQR(strings.TrimRight(s.portalURL, "/") + "/portal")
	if err != nil {
		http.Error(response, "encode the Portal QR", http.StatusInternalServerError)
		return
	}
	created, err := time.Parse(time.RFC3339, batch.CreatedAt)
	if err != nil {
		http.Error(response, "read the Voucher Batch creation time", http.StatusInternalServerError)
		return
	}
	seller := batch.Seller
	if seller == "" {
		seller = "Shop"
	}
	sheet := voucherSheet{Batch: voucherSheetBatch{
		Batch: strconv.FormatInt(batch.Number, 10), Shop: shop, Seller: seller,
		Time: voucherDuration(batch.Seconds), Validity: voucherDuration(batch.ValiditySeconds),
		PauseLimit: strconv.Itoa(batch.PauseLimit), Created: created.Local().Format("Jan 2, 2006 15:04"), QR: qrSVG,
	}, Vouchers: make([]voucherSheetCard, 0, len(vouchers))}
	// Legacy batches with no price stay blank rather than acquiring a price.
	if batch.Pesos != nil {
		sheet.Batch.Pesos = fmt.Sprintf("₱%d", *batch.Pesos)
	}
	for _, voucher := range vouchers {
		card := voucherSheetCard{Code: voucher.Code}
		if voucher.RedeemedAt != "" {
			card.Used = "USED"
		}
		sheet.Vouchers = append(sheet.Vouchers, card)
	}
	s.renderVoucherSheet(response, files, sheet)
}

func (s *Server) renderVoucherSheet(response http.ResponseWriter, files ledger.VoucherFiles, sheet voucherSheet) {
	nonceBytes := make([]byte, 16)
	if _, err := rand.Read(nonceBytes); err != nil {
		http.Error(response, "create Print sheet nonce", http.StatusInternalServerError)
		return
	}
	nonce := base64.StdEncoding.EncodeToString(nonceBytes)
	document, err := voucherDocument(files, sheet, nonce)
	if err != nil {
		http.Error(response, "build the Print sheet", http.StatusInternalServerError)
		return
	}
	// Buffer before writing headers, so a render error never returns half a sheet.
	var output bytes.Buffer
	if err := html.Render(&output, document); err != nil {
		http.Error(response, "render the Print sheet", http.StatusInternalServerError)
		return
	}
	response.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	response.Write(output.Bytes())
}

// A raw-text closing tag in CSS or custom.js must not create new HTML nodes.
var voucherRawEnd = regexp.MustCompile(`(?i)</(script|style)`)

// voucherDocument accepts files independently of their source. The template
// editor can reuse it for published files and draft previews without changing
// the batch contract or giving the HTML any script nonces.
func voucherDocument(files ledger.VoucherFiles, sheet voucherSheet, nonce string) (*html.Node, error) {
	document, err := html.Parse(strings.NewReader(files.VoucherHTML))
	if err != nil {
		return nil, err
	}
	var head, body *html.Node
	var scripts []*html.Node
	for node := range document.Descendants() {
		if node.Type != html.ElementNode {
			continue
		}
		switch node.Data {
		case "head":
			head = node
		case "body":
			body = node
		case "script":
			scripts = append(scripts, node)
		}
	}
	// Scripts belong in custom.js, never the Operator's HTML.
	for _, script := range scripts {
		script.Parent.RemoveChild(script)
	}
	data, err := json.Marshal(sheet) // Escapes </script> in Seller labels too.
	if err != nil {
		return nil, err
	}
	core, err := fs.ReadFile(staticFS, "voucher.js")
	if err != nil {
		return nil, err
	}
	element := func(tag, text string, attrs ...html.Attribute) *html.Node {
		node := &html.Node{Type: html.ElementNode, Data: tag, Attr: attrs}
		node.AppendChild(&html.Node{Type: html.TextNode, Data: text})
		return node
	}
	head.AppendChild(element("style", voucherRawEnd.ReplaceAllString(files.VoucherCSS, `<\/$1`)))
	body.AppendChild(element("script", string(data), html.Attribute{Key: "type", Val: "application/json"}, html.Attribute{Key: "data-voucher-batch"}))
	body.AppendChild(element("script", string(core), html.Attribute{Key: "nonce", Val: nonce}))
	body.AppendChild(element("script", voucherRawEnd.ReplaceAllString(files.CustomJS, `<\/$1`), html.Attribute{Key: "nonce", Val: nonce}))
	return document, nil
}

// Render the encoder's module grid directly as SVG, with a four-module quiet
// zone on all sides. The only SVG markup supplied to voucher.js is made here.
func voucherQR(portalURL string) (string, error) {
	code, err := qr.Encode(portalURL, qr.M)
	if err != nil {
		return "", err
	}
	var svg strings.Builder
	size := code.Size + 8
	fmt.Fprintf(&svg, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="Open Portal" shape-rendering="crispEdges"><rect width="%d" height="%d" fill="white"/><path fill="black" d="`, size, size, size, size)
	for y := range code.Size {
		for x := range code.Size {
			if code.Black(x, y) {
				fmt.Fprintf(&svg, "M%d %dh1v1h-1z", x+4, y+4)
			}
		}
	}
	svg.WriteString(`"/></svg>`)
	return svg.String(), nil
}

func voucherDuration(seconds int64) string {
	var parts []string
	for _, unit := range []struct {
		seconds int64
		name    string
	}{{86400, "day"}, {3600, "hour"}, {60, "minute"}, {1, "second"}} {
		if count := seconds / unit.seconds; count > 0 {
			name := unit.name
			if count != 1 {
				name += "s"
			}
			parts = append(parts, fmt.Sprintf("%d %s", count, name))
			seconds %= unit.seconds
		}
	}
	if len(parts) == 0 {
		return "0 seconds"
	}
	return strings.Join(parts, " ")
}
