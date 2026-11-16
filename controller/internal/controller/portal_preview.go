package controller

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"io/fs"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

// The Portal preview: the admin panel runs the current draft in an iframe
// sandboxed without allow-same-origin, so Operator JS gets an opaque origin and
// can't reach the admin session or the admin API. The draft's portal.css,
// core.js and custom.js are inlined into one document, and portal_preview.js,
// which runs before core.js, answers core.js's fetches with a sample state
// instead of the live Portal API. The preview CSP refuses every connection,
// and images load only from the Default Portal's own files or data: URLs.
// The API also refuses navigation requests before any Router read. The sandbox
// can't send the admin session, so the draft's cover and logo arrive as data: URLs in
// place of /portal/cover and /portal/logo.

// previewStates are the sample states, in picker order. Each name is the
// data-portal-state core.js should set for it.
var previewStates = []string{"new", "online", "coin", "paused", "out", "error"}

// previewState is the sample state named name, shaped exactly like a
// /portal/api/ response. Rates and the Points table are the site's own.
func previewState(name string, rates []ledger.RateTier, points []ledger.PointsTier) (portalState, bool) {
	now := time.Now().Unix()
	if len(rates) == 0 {
		rates = []ledger.RateTier{{Pesos: 1, Seconds: 600}, {Pesos: 5, Seconds: 3600}}
	}
	if len(points) == 0 {
		points = []ledger.PointsTier{{PointsMilli: 5000, Seconds: 1800, ValiditySeconds: 86400, PauseLimit: 1}}
	}
	state := portalState{Rates: rates, Points: points, Vendos: []portalVendo{{ID: 1, Rates: rates}}}
	online := func() *portalTimeCredit {
		return &portalTimeCredit{Bought: 3600, Remaining: 2700, UsageKnown: true, ValidUntil: now + 30*86400,
			PausesLeft: 3, PauseLimit: 3, ResumeCode: "K7QM29XD", Online: true, PointsMilli: 7500}
	}
	switch name {
	case "new":
	case "online":
		state.TimeCredit = online()
	case "coin":
		state.TimeCredit = online()
		state.CoinWindow = &portalCoinWindow{ID: 1, VendoID: 1, Status: "open", Coins: 5, ClosesAt: now + 30}
	case "paused":
		state.TimeCredit = online()
		state.TimeCredit.Paused, state.TimeCredit.Online, state.TimeCredit.PausesLeft, state.TimeCredit.ResumeCode = true, false, 2, ""
	case "out":
		state.TimeCredit = online()
		state.TimeCredit.Remaining, state.TimeCredit.Online = 0, false
		state.Message = msgTimeUp
	case "error":
		state.Message = &portalMessage{"error", "voucher-unknown", ledger.ErrVoucherUnknown.Error()}
	default:
		return portalState{}, false
	}
	return state, true
}

// portalPreview serves the current draft (or else what the editor shows) as one
// self-contained document fed the sample state ?state=.
func (s *Server) portalPreview(response http.ResponseWriter, request *http.Request) {
	rates, err := s.ledger.RateTiers()
	if err != nil {
		http.Error(response, "read the rates", http.StatusInternalServerError)
		return
	}
	points, err := s.ledger.PointsTiers()
	if err != nil {
		http.Error(response, "read the Points table", http.StatusInternalServerError)
		return
	}
	sample, ok := previewState(request.URL.Query().Get("state"), rates, points)
	if !ok {
		http.NotFound(response, request)
		return
	}
	slots, err := s.ledger.PortalState()
	if err != nil {
		http.Error(response, "read the Custom Portal", http.StatusInternalServerError)
		return
	}
	nonceBytes := make([]byte, 16)
	rand.Read(nonceBytes)
	nonce := base64.StdEncoding.EncodeToString(nonceBytes)
	document, err := previewDocument(editorFiles(slots), sample, request.URL.Query().Get("state"), nonce)
	if err != nil {
		http.Error(response, "build the preview", http.StatusInternalServerError)
		return
	}
	// Images by exact URL: a /portal/api/ path can't load as one.
	images := []string{"data:"}
	files, _ := fs.ReadDir(portalFS, ".")
	for _, file := range files {
		if name := file.Name(); !strings.HasSuffix(name, ".html") && !strings.HasSuffix(name, ".js") && !strings.HasSuffix(name, ".css") {
			images = append(images, "http://"+request.Host+"/portal/"+name)
		}
	}
	response.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'nonce-"+nonce+"'; "+
		"img-src "+strings.Join(images, " ")+"; form-action 'none'; frame-ancestors 'self'; base-uri 'none'")
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	html.Render(response, document)
}

// previewImageURL is a CSS url() naming the cover or logo slot.
var previewImageURL = regexp.MustCompile(`url\(\s*['"]?/portal/(logo|cover)['"]?\s*\)`)

// previewDocument inlines the draft's stylesheet and scripts into its page,
// with nonces, and its cover and logo as data: URLs. A deferred script moves to the end of <body>, where an inline
// script runs at about the same time; portal_preview.js goes just before core.js.
func previewDocument(files ledger.PortalFiles, sample portalState, name, nonce string) (*html.Node, error) {
	document, err := html.Parse(strings.NewReader(files.IndexHTML))
	if err != nil {
		return nil, err
	}
	harness, err := fs.ReadFile(staticFS, "portal_preview.js")
	if err != nil {
		return nil, err
	}
	core, err := fs.ReadFile(portalFS, "core.js")
	if err != nil {
		return nil, err
	}
	state, err := json.Marshal(sample)
	if err != nil {
		return nil, err
	}
	images := map[string]string{} // slot name -> data: URL
	for name, image := range portalImages {
		images[name] = image.dataURL(*image.file(&files))
	}
	css := previewImageURL.ReplaceAllStringFunc(files.PortalCSS, func(match string) string {
		return `url("` + images[previewImageURL.FindStringSubmatch(match)[1]] + `")`
	})
	inline := func(tag atom.Atom, content string, attributes ...html.Attribute) *html.Node {
		node := &html.Node{Type: html.ElementNode, DataAtom: tag, Data: tag.String(),
			Attr: append(attributes, html.Attribute{Key: "nonce", Val: nonce})}
		// Raw text: only a closing tag could end it early.
		content = strings.ReplaceAll(content, "</"+tag.String(), `<\/`+tag.String())
		node.AppendChild(&html.Node{Type: html.TextNode, Data: content})
		return node
	}

	// Collected first, then applied: the walk must not see its own edits.
	var body *html.Node
	var replaced []*html.Node
	var inlined, deferred [][]*html.Node // per replaced node: what goes in its place, or at the end of <body>
	for node := range document.Descendants() {
		if node.Type != html.ElementNode {
			continue
		}
		attributes := map[string]string{}
		for i, a := range node.Attr {
			attributes[a.Key] = a.Val
			if name, ok := strings.CutPrefix(a.Val, "/portal/"); ok && images[name] != "" {
				node.Attr[i].Val = images[name]
			}
		}
		var nodes []*html.Node
		switch {
		case node.DataAtom == atom.Body:
			body = node
		case node.DataAtom == atom.Link && attributes["href"] == "/portal/portal.css":
			nodes = []*html.Node{inline(atom.Style, css)}
		case node.DataAtom == atom.Script && attributes["src"] == "/portal/core.js":
			nodes = []*html.Node{
				inline(atom.Script, string(harness), html.Attribute{Key: "data-state", Val: string(state)}, html.Attribute{Key: "data-name", Val: name}),
				inline(atom.Script, string(core)),
			}
		case node.DataAtom == atom.Script && attributes["src"] == "/portal/custom.js":
			nodes = []*html.Node{inline(atom.Script, files.CustomJS)}
		}
		if nodes == nil {
			continue
		}
		_, isDeferred := attributes["defer"]
		_, isAsync := attributes["async"]
		replaced = append(replaced, node)
		if node.DataAtom == atom.Script && (isDeferred || isAsync) {
			inlined, deferred = append(inlined, nil), append(deferred, nodes)
		} else {
			inlined, deferred = append(inlined, nodes), append(deferred, nil)
		}
	}
	for i, node := range replaced {
		for _, next := range inlined[i] {
			node.Parent.InsertBefore(next, node)
		}
		for _, next := range deferred[i] {
			body.AppendChild(next)
		}
		node.Parent.RemoveChild(node)
	}
	return document, nil
}
