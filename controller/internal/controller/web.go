package controller

import (
	"bytes"
	"encoding/json"
	"html/template"
	"io/fs"
	"net/http"
	"slices"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

// The admin UI is server-rendered html/template plus vendored htmx and the
// Alpine CSP build. web_embed.go embeds them in the binary; the dev build
// (web_dev.go) reads web/ from disk so edits show on refresh.

const adminCSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

// portalCSP keeps every Portal load and connection on the Controller ('self')
// and refuses inline styles and scripts; see docs/portal-contract.md.
const portalCSP = "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

// sharedTemplates are parsed into every page set: the layout and the partials
// more than one page renders.
var sharedTemplates = []string{"layout.html", "tier_editor.html", "checklist.html"}

// parsePages builds one template set per page file: sharedTemplates plus <page>.html.
// Each page file defines "content" (and any named fragments); sets are separate so
// every page can reuse the name "content". A bad template fails at startup.
func parsePages() map[string]*template.Template {
	funcs := template.FuncMap{
		"icon":                 icon,
		"voucherAlphabet":      ledger.VoucherCodeAlphabet,
		"voucherCodeSpaceSize": ledger.VoucherCodeSpaceSize,
		"asset":                func(name string) string { return "/admin/static/" + name + "?v=" + assetVersion },
		"previewStates":        func() []string { return previewStates },
		"duration":             duration,        // compact, for dense tables: 2h 30m
		"longDuration":         voucherDuration, // spelled out: 2 hours 30 minutes
		"stamp":                localStamp,
	}
	files, err := fs.Glob(templateFS, "*.html")
	if err != nil {
		panic(err)
	}
	pages := map[string]*template.Template{}
	for _, file := range files {
		if slices.Contains(sharedTemplates, file) {
			continue
		}
		name := strings.TrimSuffix(file, ".html")
		pages[name] = template.Must(template.New(name).Funcs(funcs).ParseFS(templateFS, append(slices.Clone(sharedTemplates), file)...))
	}
	return pages
}

// Page is what every admin page handler hands to renderPage.
type Page struct {
	Title   string // <title> and breadcrumb leaf
	Section string // breadcrumb parent, e.g. "Main" or "Settings"
	Active  string // Href of the nav item to highlight, query included for sub-items
	Data    any    // page-specific view, the dot inside the page's "content" template
}

// layoutView is the dot of the "layout" and "boosted" templates.
type layoutView struct {
	Page
	Platform  []navItem
	Quick     []navItem
	Secondary []navItem
	RouterOK  bool
	Palette   string // JSON []searchItem for the command palette's static entries
}

// renderPage writes a full admin page: the shared layout around the page's
// "content" template. For a boosted navigation (hx-boost links in the sidebar
// send HX-Boosted) it writes only <title>, the out-of-band breadcrumb and the
// content, which htmx swaps into #view. A history-restore refetch still gets the
// full page.
//
// Handlers that serve both a page and a fragment at one URL branch first:
//
//	if isFragmentRequest(r) {
//		renderFragment(w, "time_credits", "list", data)
//		return
//	}
//	s.renderPage(w, r, "time_credits", Page{...})
func (s *Server) renderPage(response http.ResponseWriter, request *http.Request, name string, page Page) {
	s.mu.Lock()
	routerOK := s.routerStatus == "ready"
	s.mu.Unlock()
	view := layoutView{Page: page, RouterOK: routerOK}
	view.Platform, view.Quick, view.Secondary = platformNav(), quickNav(), secondaryNav()
	for _, group := range [][]navItem{view.Platform, view.Quick, view.Secondary} {
		markActive(group, page.Active)
	}
	palette, _ := json.Marshal(paletteItems(view.Platform, view.Quick))
	view.Palette = string(palette)

	entry := "layout"
	if request.Header.Get("HX-Boosted") == "true" && request.Header.Get("HX-History-Restore-Request") == "" {
		entry = "boosted"
	}
	execute(response, name, entry, view)
}

// renderFragment writes one named template from a page file (for example a table
// body or list) without any layout. Use it for endpoints that only ever return
// a fragment and for the HX-Request branch of dual-purpose handlers. The standalone
// login document is also rendered this way.
func renderFragment(response http.ResponseWriter, name, fragment string, data any) {
	execute(response, name, fragment, data)
}

// isFragmentRequest reports whether htmx asked for part of a page (a poll, an
// hx-get into a target) rather than a navigation. Boosted navigations and
// history restores are not fragment requests; renderPage handles them.
func isFragmentRequest(request *http.Request) bool {
	return request.Header.Get("HX-Request") == "true" &&
		request.Header.Get("HX-Boosted") != "true" &&
		request.Header.Get("HX-History-Restore-Request") == ""
}

// execute renders into a buffer first so a template error becomes a clean 500
// instead of a half-written page.
func execute(response http.ResponseWriter, name, entry string, data any) {
	var out bytes.Buffer
	if err := pageTemplate(name).ExecuteTemplate(&out, entry, data); err != nil {
		http.Error(response, "render "+name, http.StatusInternalServerError)
		return
	}
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	response.Write(out.Bytes())
}

// staticHandler serves the assets. In release builds they are content-versioned by the ?v=
// query, so they may be cached forever; misses and directory listings are 404 and
// must not get the cache header.
func staticHandler(response http.ResponseWriter, request *http.Request) {
	name := strings.TrimPrefix(request.URL.Path, "/admin/static/")
	if info, err := fs.Stat(staticFS, name); err != nil || info.IsDir() {
		http.NotFound(response, request)
		return
	}
	response.Header().Set("Cache-Control", staticCacheControl)
	staticFiles.ServeHTTP(response, request)
}

type navItem struct {
	Label  string
	Href   string
	Icon   string
	Subs   []navItem
	Active bool // this item is the current page
	Open   bool // this item or one of its Subs is the current page
}

func platformNav() []navItem {
	return []navItem{
		{Label: "Dashboard", Href: "/admin/dashboard", Icon: "layout-dashboard"},
		{Label: "Sales Inventory", Href: "/admin/sales", Icon: "receipt"},
		{Label: "Vendo List", Href: "/admin/vendos", Icon: "cpu"},
		// Shop holds the defaults a Vendo can override (GLOSSARY "Shop").
		{Label: "Shop", Href: "/admin/rates", Icon: "store", Subs: []navItem{
			{Label: "Rate tiers", Href: "/admin/rates"},
			{Label: "Points table", Href: "/admin/points"},
		}},
		{Label: "Time Credits", Href: "/admin/time-credits", Icon: "timer", Subs: []navItem{
			{Label: "Online now", Href: "/admin/time-credits?state=online"},
			{Label: "Paused", Href: "/admin/time-credits?state=paused"},
			{Label: "Expired", Href: "/admin/time-credits?state=expired"},
		}},
		{Label: "Logs", Href: "/admin/logs", Icon: "activity"},
		{Label: "Coins", Href: "/admin/coins", Icon: "coins", Subs: []navItem{
			{Label: "Coin sales", Href: "/admin/coins"},
			{Label: "Credit intents", Href: "/admin/intents"},
		}},
		{Label: "Vouchers", Href: "/admin/vouchers", Icon: "ticket"},
		{Label: "Settings", Href: "/admin/pause", Icon: "settings-2", Subs: []navItem{
			{Label: "Pause", Href: "/admin/pause"},
			{Label: "Validity", Href: "/admin/validity"},
			{Label: "Portal", Href: "/admin/portal"},
			{Label: "Voucher template", Href: "/admin/voucher-template"},
			{Label: "Router setup", Href: "/admin"},
		}},
	}
}

func quickNav() []navItem {
	return []navItem{
		{Label: "Grant time", Href: "/admin/time-credits", Icon: "circle-plus"},
		{Label: "Generate vouchers", Href: "/admin/vouchers", Icon: "ticket"},
		{Label: "Pair a Vendo", Href: "/admin/vendos", Icon: "cpu"},
	}
}

func secondaryNav() []navItem {
	return []navItem{{Label: "Router status", Href: "/admin", Icon: "router"}}
}

// markActive sets Active/Open from the page's Active href. A sub-item that
// shares its parent's Href takes the highlight, so only one entry lights up.
// A fragment in a Href never matches a page.
func markActive(items []navItem, active string) {
	for i := range items {
		item := &items[i]
		item.Active = item.Href == active
		item.Open = item.Active
		markActive(item.Subs, active)
		for _, sub := range item.Subs {
			if sub.Active {
				item.Active, item.Open = false, true
			}
		}
	}
}

// searchItem is one command-palette entry. It is also the JSON shape of
// GET /admin/search: a JSON array of {"label","hint","href"}, where hint is a
// short group name shown beside the label and href is an /admin path to open.
type searchItem struct {
	Label string `json:"label"`
	Hint  string `json:"hint"`
	Href  string `json:"href"`
}

// paletteItems flattens the nav into the palette's static entries. Every sub is
// its own entry, hinted with its group, even when it shares the group's Href.
func paletteItems(platform, quick []navItem) []searchItem {
	items := []searchItem{}
	for _, item := range platform {
		items = append(items, searchItem{item.Label, "Page", item.Href})
		for _, sub := range item.Subs {
			items = append(items, searchItem{sub.Label, item.Label, sub.Href})
		}
	}
	for _, item := range quick {
		items = append(items, searchItem{item.Label, "Quick action", item.Href})
	}
	return items
}
