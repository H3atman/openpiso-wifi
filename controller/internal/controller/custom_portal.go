package controller

import (
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"golang.org/x/net/html"
)

// The Custom Portal: an Operator's edit of the Default Portal's index.html,
// portal.css and optional custom.js, plus a cover and a logo, saved as a draft
// through the save check and published from the admin panel. core.js is always
// the embedded one. The save check catches mistakes; the Portal CSP, which every
// /portal response carries, is the security boundary.

const portalFileLimit = 64 << 10

// portalImage is one of the two image slots, served at /portal/{logo,cover}.
// The admin's browser crops and encodes the WebP; the Controller checks only its
// magic bytes and the cap, and does no image work.
type portalImage struct {
	limit   int
	builtin string // the embedded image served while the slot is empty
	file    func(*ledger.PortalFiles) *[]byte
}

var portalImages = map[string]portalImage{
	"logo":  {limit: 20 << 10, builtin: "logo.svg", file: func(f *ledger.PortalFiles) *[]byte { return &f.Logo }},
	"cover": {limit: 100 << 10, builtin: "cover.svg", file: func(f *ledger.PortalFiles) *[]byte { return &f.Cover }},
}

// dataURL is the stored WebP, or the built-in image when there is none, as a data: URL.
func (image portalImage) dataURL(content []byte) string {
	if len(content) == 0 {
		builtin, err := fs.ReadFile(portalFS, image.builtin)
		if err != nil {
			panic(err) // embedded at build time
		}
		return "data:image/svg+xml;base64," + base64.StdEncoding.EncodeToString(builtin)
	}
	return "data:image/webp;base64," + base64.StdEncoding.EncodeToString(content)
}

// serve writes the stored WebP, or the built-in image when there is none.
func (image portalImage) serve(response http.ResponseWriter, request *http.Request, content []byte) {
	if len(content) == 0 {
		http.ServeFileFS(response, request, portalFS, image.builtin)
		return
	}
	response.Header().Set("Content-Type", "image/webp")
	response.Write(content)
}

// portalMarker is a marker the money and recovery paths need; see
// docs/portal-contract.md. The Default Portal places insert-coin and connect
// once per state, so those two may repeat.
type portalMarker struct {
	attribute, name string
	form            bool // must be on a <form>
	repeats         bool
}

var requiredMarkers = []portalMarker{
	{attribute: "data-portal", name: "remaining"},
	{attribute: "data-portal", name: "message"},
	{attribute: "data-portal", name: "resume-code"},
	{attribute: "data-action", name: "insert-coin", repeats: true},
	{attribute: "data-action", name: "coins-done"},
	{attribute: "data-action", name: "redeem", form: true},
	{attribute: "data-action", name: "recover", form: true},
	{attribute: "data-action", name: "pause"},
	{attribute: "data-action", name: "resume"},
	{attribute: "data-action", name: "connect", repeats: true},
}

func (m portalMarker) String() string {
	if m.form {
		return fmt.Sprintf(`<form %s=%q>`, m.attribute, m.name)
	}
	return fmt.Sprintf(`%s=%q`, m.attribute, m.name)
}

var (
	absoluteURL = regexp.MustCompile(`(?i)\bhttps?:[^\s"'<>()]*`)
	// Protocol-relative URLs, where each language puts them. "//" alone is a JS comment.
	relativeURL = map[string]*regexp.Regexp{
		"portal.css": regexp.MustCompile(`(?i)(?:url\(\s*['"]?|@import\s*['"])\s*(//[^\s'")]*)`),
		"custom.js":  regexp.MustCompile("[\"'`](//[^\\s\"'`]*)"),
	}
)

// checkPortal is the save check. It returns one message per failure, saying
// exactly what failed; none means the draft may be saved.
func checkPortal(files ledger.PortalFiles) []string {
	named := []struct{ name, content string }{
		{"index.html", files.IndexHTML}, {"portal.css", files.PortalCSS}, {"custom.js", files.CustomJS},
	}
	var failures []string
	external := func(name, url string) {
		failures = append(failures, fmt.Sprintf("%s: external URL %q; the Portal loads only from the Controller", name, url))
	}
	for _, file := range named {
		if len(file.content) > portalFileLimit {
			failures = append(failures, fmt.Sprintf("%s is %d bytes; each file must be 64 KB (%d bytes) or less", file.name, len(file.content), portalFileLimit))
		}
		if !utf8.ValidString(file.content) {
			failures = append(failures, file.name+" is not valid UTF-8")
		}
		if file.name != "index.html" {
			for _, url := range absoluteURL.FindAllString(file.content, -1) {
				external(file.name, url)
			}
		}
		if pattern := relativeURL[file.name]; pattern != nil {
			for _, match := range pattern.FindAllStringSubmatch(file.content, -1) {
				external(file.name, match[1])
			}
		}
	}

	document, err := html.Parse(strings.NewReader(files.IndexHTML))
	if err != nil {
		return append(failures, "index.html does not parse: "+err.Error())
	}
	counts := map[portalMarker]int{}
	nonForms := map[portalMarker]bool{}
	core := 0
	for node := range document.Descendants() {
		if node.Type != html.ElementNode {
			for _, url := range absoluteURL.FindAllString(node.Data, -1) {
				external("index.html", url)
			}
			continue
		}
		for _, attribute := range node.Attr {
			// Namespace declarations name XML vocabularies; resource URLs still fail.
			namespace := attribute.Key == "xmlns" || strings.HasPrefix(attribute.Key, "xmlns:") || attribute.Namespace == "xmlns"
			for _, url := range absoluteURL.FindAllString(attribute.Val, -1) {
				if !namespace || url != attribute.Val || !strings.HasPrefix(url, "http://www.w3.org/") {
					external("index.html", url)
				}
			}
			if strings.HasPrefix(strings.TrimSpace(attribute.Val), "//") {
				external("index.html", strings.TrimSpace(attribute.Val))
			}
			if node.Data == "script" && attribute.Key == "src" && attribute.Val == "/portal/core.js" {
				core++
			}
			if attribute.Key == "data-portal" || attribute.Key == "data-action" {
				marker := portalMarker{attribute: attribute.Key, name: attribute.Val}
				counts[marker]++
				if node.Data != "form" {
					nonForms[marker] = true
				}
			}
		}
	}
	if core != 1 {
		failures = append(failures, fmt.Sprintf(`core.js must be loaded exactly once with <script src="/portal/core.js">; found %d`, core))
	}
	for _, marker := range requiredMarkers {
		key := portalMarker{attribute: marker.attribute, name: marker.name}
		count := counts[key]
		switch {
		case count == 0:
			failures = append(failures, fmt.Sprintf("required marker %s is missing", marker))
		case count > 1 && !marker.repeats:
			failures = append(failures, fmt.Sprintf("required marker %s appears %d times; it must appear exactly once", marker, count))
		case marker.form && nonForms[key]:
			failures = append(failures, fmt.Sprintf("required marker %s must be on a <form>", marker))
		}
	}
	return failures
}

// publishedPortal is the Custom Portal Devices get, or nil for the Default Portal.
func (s *Server) publishedPortal(response http.ResponseWriter) (*ledger.PortalVersion, bool) {
	published, err := s.ledger.PortalVersion(ledger.PortalPublished)
	if err != nil {
		http.Error(response, "Portal temporarily unavailable", http.StatusServiceUnavailable)
		return nil, false
	}
	return published, true
}

// portalPage serves the published Custom Portal's page, or the Default
// Portal's; core.js does the rest through /portal/api/.
func (s *Server) portalPage(response http.ResponseWriter, request *http.Request) {
	published, ok := s.publishedPortal(response)
	if !ok {
		return
	}
	if published == nil {
		http.ServeFileFS(response, request, portalFS, "index.html")
		return
	}
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	response.Write([]byte(published.Files.IndexHTML))
}

// portalAsset serves the Portal's flat files (script, stylesheet, images). A
// published Custom Portal replaces portal.css, custom.js and, when it has them,
// the cover and logo; core.js and the other images stay embedded. The page
// links them unversioned, so they keep the default no-store. The single-segment
// {file} pattern leaves GETs on POST-only /portal/api/ routes to answer 405.
func (s *Server) portalAsset(response http.ResponseWriter, request *http.Request) {
	name := request.PathValue("file")
	image, isImage := portalImages[name]
	if isImage || name == "portal.css" || name == "custom.js" {
		published, ok := s.publishedPortal(response)
		if !ok {
			return
		}
		if isImage {
			var content []byte
			if published != nil {
				content = *image.file(&published.Files)
			}
			image.serve(response, request, content)
			return
		}
		if published != nil {
			if name == "portal.css" {
				response.Header().Set("Content-Type", "text/css; charset=utf-8")
				response.Write([]byte(published.Files.PortalCSS))
			} else {
				response.Header().Set("Content-Type", "text/javascript; charset=utf-8")
				response.Write([]byte(published.Files.CustomJS))
			}
			return
		}
	}
	if info, err := fs.Stat(portalFS, name); err != nil || info.IsDir() {
		http.NotFound(response, request)
		return
	}
	portalFiles.ServeHTTP(response, request)
}

type portalEditorView struct {
	Files    ledger.PortalFiles
	Slots    ledger.PortalState
	Failures []string
	Saved    int64 // the draft version just saved
}

// portalEditor renders the editor on files, or else on editorFiles.
func (s *Server) portalEditor(response http.ResponseWriter, request *http.Request, view portalEditorView) {
	slots, err := s.ledger.PortalState()
	if err != nil {
		http.Error(response, "read the Custom Portal", http.StatusInternalServerError)
		return
	}
	view.Slots = slots
	if view.Failures == nil && view.Saved == 0 {
		view.Files = editorFiles(slots)
	}
	if view.Failures != nil {
		response.Header().Set("Content-Type", "text/html; charset=utf-8")
		response.WriteHeader(http.StatusBadRequest)
	}
	s.renderPage(response, request, "portal", Page{Title: "Portal", Section: "Settings", Active: "/admin/portal", Data: view})
}

// editorFiles is what the editor opens, the preview runs and a save or image
// upload builds on: the draft, the published version or the Default Portal,
// whichever exists first.
func editorFiles(slots ledger.PortalState) ledger.PortalFiles {
	switch {
	case slots.Draft != nil:
		return slots.Draft.Files
	case slots.Published != nil:
		return slots.Published.Files
	}
	return defaultPortalFiles()
}

func (s *Server) portalEditorPage(response http.ResponseWriter, request *http.Request) {
	s.portalEditor(response, request, portalEditorView{})
}

func defaultPortalFiles() ledger.PortalFiles {
	read := func(name string) string {
		content, err := fs.ReadFile(portalFS, name)
		if err != nil {
			panic(err) // embedded at build time
		}
		return string(content)
	}
	return ledger.PortalFiles{IndexHTML: read("index.html"), PortalCSS: read("portal.css"), CustomJS: read("custom.js")}
}

// downloadDefaultPortal is "Download default": one Default Portal file as an attachment.
func downloadDefaultPortal(response http.ResponseWriter, request *http.Request) {
	name := request.PathValue("file")
	if name != "index.html" && name != "portal.css" && name != "custom.js" {
		http.NotFound(response, request)
		return
	}
	content, err := fs.ReadFile(portalFS, name)
	if err != nil {
		http.NotFound(response, request)
		return
	}
	// Written directly: ServeFileFS redirects a path ending in /index.html.
	response.Header().Set("Content-Type", "application/octet-stream")
	response.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
	response.Write(content)
}

// savePortalDraft runs the save check and stores a passing draft, keeping the
// draft's cover and logo. A failing draft is shown back in the editor with every
// failure, and nothing is stored.
func (s *Server) savePortalDraft(response http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(response, "the draft is too large to save", http.StatusRequestEntityTooLarge)
		return
	}
	files := ledger.PortalFiles{
		IndexHTML: request.PostForm.Get("index_html"),
		PortalCSS: request.PostForm.Get("portal_css"),
		CustomJS:  request.PostForm.Get("custom_js"),
	}
	if failures := checkPortal(files); failures != nil {
		s.portalEditor(response, request, portalEditorView{Files: files, Failures: failures})
		return
	}
	slots, err := s.ledger.PortalState()
	if err != nil {
		http.Error(response, "read the Custom Portal", http.StatusInternalServerError)
		return
	}
	base := editorFiles(slots)
	files.Logo, files.Cover = base.Logo, base.Cover
	version, err := s.ledger.SavePortalDraft(files)
	if err != nil {
		http.Error(response, "save the draft", http.StatusInternalServerError)
		return
	}
	s.portalEditor(response, request, portalEditorView{Files: files, Saved: version})
}

// draftPortalImage shows the admin page the cover or logo of editorFiles.
func (s *Server) draftPortalImage(response http.ResponseWriter, request *http.Request) {
	image, ok := portalImages[request.PathValue("image")]
	if !ok {
		http.NotFound(response, request)
		return
	}
	slots, err := s.ledger.PortalState()
	if err != nil {
		http.Error(response, "read the Custom Portal", http.StatusInternalServerError)
		return
	}
	base := editorFiles(slots)
	image.serve(response, request, *image.file(&base))
}

// uploadPortalImage stores the admin page's cropped WebP, the raw request body,
// in a new draft built on the current one.
func (s *Server) uploadPortalImage(response http.ResponseWriter, request *http.Request) {
	name := request.PathValue("image")
	image, ok := portalImages[name]
	if !ok {
		http.NotFound(response, request)
		return
	}
	content, err := io.ReadAll(io.LimitReader(request.Body, int64(image.limit)+1))
	if err != nil {
		http.Error(response, "read the "+name, http.StatusBadRequest)
		return
	}
	if len(content) > image.limit {
		http.Error(response, fmt.Sprintf("the %s must be %d KB (%d bytes) or less", name, image.limit>>10, image.limit), http.StatusRequestEntityTooLarge)
		return
	}
	if len(content) < 12 || string(content[:4]) != "RIFF" || string(content[8:12]) != "WEBP" {
		http.Error(response, "the "+name+" must be a WebP image", http.StatusUnsupportedMediaType)
		return
	}
	slots, err := s.ledger.PortalState()
	if err != nil {
		http.Error(response, "read the Custom Portal", http.StatusInternalServerError)
		return
	}
	files := editorFiles(slots)
	*image.file(&files) = content
	version, err := s.ledger.SavePortalDraft(files)
	if err != nil {
		http.Error(response, "save the draft", http.StatusInternalServerError)
		return
	}
	fmt.Fprintf(response, "Draft saved (version %d).", version)
}

// portalSlotAction answers Publish, Revert and Reset to default.
func (s *Server) portalSlotAction(change func() error) http.HandlerFunc {
	return func(response http.ResponseWriter, request *http.Request) {
		err := change()
		if errors.Is(err, ledger.ErrNoPortalDraft) || errors.Is(err, ledger.ErrNoPortalPrevious) {
			http.Error(response, err.Error(), http.StatusConflict)
			return
		}
		if err != nil {
			http.Error(response, "change the Custom Portal", http.StatusInternalServerError)
			return
		}
		http.Redirect(response, request, "/admin/portal", http.StatusSeeOther)
	}
}
