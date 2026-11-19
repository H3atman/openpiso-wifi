package controller

import (
	"encoding/json"
	"net/http"
	"net/url"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/hoststatus"
)

// The Dashboard: a glanceable summary. One dashboardView feeds the full page,
// the 10-second poll fragment and ?format=json, so the three never disagree.

// salesTotals are the calendar-period-to-date Sales in pesos, local time.
type salesTotals struct {
	Today int64 `json:"today"`
	Week  int64 `json:"week"` // from Monday
	Month int64 `json:"month"`
	Year  int64 `json:"year"`
}

// salesTile is one Sales tile; Href opens the Sales page on the same period.
type salesTile struct {
	Label string
	Pesos int64
	Href  string
}

// dashboardController is the Controller System Status block: the host metrics
// (each null in JSON when unreadable) plus the card rows rendered from them.
type dashboardController struct {
	hoststatus.Status
	Version string        `json:"version"`
	Update  *updateNotice `json:"update"` // null without the root update helper (dev runs)
	Rows    []statusRow   `json:"-"`
}

// controllerStatus reads the host and lays out its card rows.
func (s *Server) controllerStatus() dashboardController {
	status := s.host.Read()
	rows := []statusRow{
		{Label: "Version", Value: s.version},
		{"CPU", formatPercent(status.CPUPercent), loadBar(status.CPUPercent)},
		{"Memory", formatFreeOf(status.MemoryFree, status.MemoryTotal), usedBar(status.MemoryFree, status.MemoryTotal)},
		{"Disk", formatFreeOf(status.DiskFree, status.DiskTotal), usedBar(status.DiskFree, status.DiskTotal)},
		{Label: "Temperature", Value: formatCelsius(status.TemperatureC)},
		{Label: "Uptime", Value: formatUptime(status.UptimeSeconds)},
	}
	return dashboardController{Status: status, Version: s.version, Update: s.readUpdateNotice(), Rows: rows}
}

// dashboardView is the Dashboard data and, as-is, the ?format=json body. A nil
// count is unknown (null in JSON).
type dashboardView struct {
	Sales                 salesTotals         `json:"sales"`
	ActiveHotspot         *int                `json:"active_hotspot"`           // Time Credits with a live Router HotSpot session
	CustomersWithTimeLeft *int                `json:"customers_with_time_left"` // online + paused Time Credits
	PendingAudit          int                 `json:"pending_audit"`            // coin reports awaiting review
	RouterSetupFinished   bool                `json:"router_setup_finished"`    // a HotSpot server is Adopted, or will be at boot
	Controller            dashboardController `json:"controller"`
	Router                dashboardRouter     `json:"router"`
	SalesTiles            []salesTile         `json:"-"`
}

// dashboard reads everything the Dashboard shows. Router trouble only blanks the
// counts that need it; Sales and the audit count come from the ledger alone.
func (s *Server) dashboard() (dashboardView, error) {
	var view dashboardView
	view.Controller = s.controllerStatus()
	periods := salesPeriods(time.Now())
	totals := []*int64{&view.Sales.Today, &view.Sales.Week, &view.Sales.Month, &view.Sales.Year}

	view.Router = s.readRouterStatus() // a network read: keep it outside voucherMu

	s.voucherMu.Lock()
	defer s.voucherMu.Unlock()
	for index, period := range periods {
		report, err := s.ledger.Sales(period.From, period.To.AddDate(0, 0, 1))
		if err != nil {
			return view, err
		}
		*totals[index] = report.Total()
		href := "/admin/sales?" + url.Values{"from": {period.From.Format(salesDateLayout)}, "to": {period.To.Format(salesDateLayout)}}.Encode()
		view.SalesTiles = append(view.SalesTiles, salesTile{period.Label, report.Total(), href})
	}

	adopted, err := s.ledger.AdoptedHotspotServers()
	if err != nil {
		return view, err
	}
	pending, err := s.ledger.AdoptAllPending()
	if err != nil {
		return view, err
	}
	view.RouterSetupFinished = len(adopted) > 0 || pending

	review, err := s.ledger.AuditReview()
	if err != nil {
		return view, err
	}
	for _, item := range review {
		if item.Status == "pending-review" {
			view.PendingAudit++
		}
	}

	rows, err := s.ledger.ListTimeCredits()
	if err != nil {
		return view, err
	}
	snapshot := s.usageSnapshot()
	var online, timeLeft, undecided int
	for _, credit := range creditViews(rows, snapshot) {
		switch credit.State {
		case "online":
			online++
			timeLeft++
		case "paused":
			timeLeft++
		case "unknown":
			undecided++ // no Router reading ever: it may or may not have time left
		}
	}
	if snapshot.Known && !snapshot.Stale && !view.Router.Unreachable {
		view.ActiveHotspot = &online
	}
	if undecided == 0 {
		view.CustomersWithTimeLeft = &timeLeft
	}
	return view, nil
}

// dashboardPage serves GET /admin/dashboard, or its JSON with ?format=json.
func (s *Server) dashboardPage(response http.ResponseWriter, request *http.Request) {
	view, err := s.dashboard()
	if err != nil {
		http.Error(response, "read dashboard", http.StatusInternalServerError)
		return
	}
	if request.URL.Query().Get("format") == "json" {
		response.Header().Set("Content-Type", "application/json")
		json.NewEncoder(response).Encode(view)
		return
	}
	s.renderPage(response, request, "dashboard", Page{Title: "Dashboard", Section: "Main", Active: "/admin/dashboard", Data: view})
}

// dashboardFragment is the page's 10-second poll: the same view as the page body.
func (s *Server) dashboardFragment(response http.ResponseWriter, _ *http.Request) {
	view, err := s.dashboard()
	if err != nil {
		http.Error(response, "read dashboard", http.StatusInternalServerError)
		return
	}
	renderFragment(response, "dashboard", "dashboard_body", view)
}
