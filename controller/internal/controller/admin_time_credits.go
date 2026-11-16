package controller

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

// The Time Credits page, its list/detail fragments and the command-palette
// search. All of them read the ledger plus the shared Router usage snapshot
// (usage_cache.go); none takes voucherMu.

// tcView is one Time Credit as the admin UI shows it. Used and remaining time
// come only from the Router; Metered is false when it reported none (Router
// unreachable, or the credit has no Router user, such as an expired one).
type tcView struct {
	ID         string
	MAC        string
	IP         string
	Bought     int64
	Points     string // balance, read-only: there is no grant or deduct
	Used       int64
	Remaining  int64
	Metered    bool
	State      string // online, paused, expired, offline or unknown
	ValidUntil int64
	Expired    bool
	Href       string // the credit's page URL; carries the list filters in the split view
	DetailURL  string // htmx fragment URL for the detail pane
	Current    bool   // selected in the split view
}

func (v tcView) BoughtText() string { return duration(v.Bought) }

func (v tcView) UsedText() string {
	if !v.Metered {
		return "—"
	}
	return duration(v.Used)
}

func (v tcView) RemainingText() string {
	if !v.Metered {
		return "—"
	}
	return duration(v.Remaining)
}

// ShowBar reports whether the remaining-time progress bar has something to draw.
func (v tcView) ShowBar() bool { return v.Metered && v.Bought > 0 }

func (v tcView) ValidityText() string {
	if v.Expired {
		return "expired"
	}
	return "in " + duration(v.ValidUntil-time.Now().Unix())
}

func (v tcView) Device() string {
	if v.MAC == "" && v.IP == "" {
		return "unbound"
	}
	return v.MAC + " · " + v.IP
}

// duration renders seconds as the two largest units, for example 2d 3h, 1h 05m, 4m 09s.
func duration(seconds int64) string {
	switch {
	case seconds >= 86400:
		return fmt.Sprintf("%dd %dh", seconds/86400, seconds%86400/3600)
	case seconds >= 3600:
		return fmt.Sprintf("%dh %02dm", seconds/3600, seconds%3600/60)
	case seconds >= 60:
		return fmt.Sprintf("%dm %02ds", seconds/60, seconds%60)
	}
	return fmt.Sprintf("%ds", max(seconds, 0))
}

// creditViews joins ledger rows with the Router snapshot. State precedence:
// expired (Validity ended, or the Router meter is used up), paused (tapped),
// online (active Router session), unknown (no Router reading ever), offline.
func creditViews(rows []ledger.TimeCreditRow, snapshot usageSnapshot) []tcView {
	views := make([]tcView, 0, len(rows))
	for _, row := range rows {
		used, metered := snapshot.Usage.UsedSeconds[row.ID]
		view := tcView{ID: row.ID, MAC: row.MAC, IP: row.IP, Bought: row.BoughtSeconds, Points: pointsText(row.PointsMilli), Used: used, Metered: metered,
			Remaining: max(int64(0), row.BoughtSeconds-used), ValidUntil: row.ValidUntil, Expired: row.Expired()}
		switch {
		case view.Expired || (metered && row.BoughtSeconds > 0 && view.Remaining == 0):
			view.State = "expired"
		case row.Paused:
			view.State = "paused"
		case snapshot.Usage.Active[row.ID]:
			view.State = "online"
		case !snapshot.Known:
			view.State = "unknown"
		default:
			view.State = "offline"
		}
		view.Href = "/admin/time-credits?" + url.Values{"tc": {row.ID}}.Encode()
		views = append(views, view)
	}
	return views
}

// timelineLine is one rendered history entry.
type timelineLine struct {
	Kind, Time, Text string
}

// creditsView is the dot of every Time Credits template. The URL (tc, q, state,
// tab) is the single source of truth: the same view renders the full page, the
// list fragment and the detail fragment.
type creditsView struct {
	Query, State, Tab string
	Total             int // all Time Credits, before filtering
	List              []tcView
	Selected          *tcView
	Missing           string // ?tc= that matches no Time Credit
	Timeline          []timelineLine
	RequestID         string // idempotency key for the Top-up form
	Audit             []ledger.AuditReview
	Known, Stale      bool
}

func (v creditsView) AuditTab() bool { return v.Tab == "audit" }

// tabHref links a tab, keeping the selected credit and filters.
func (v creditsView) tabHref(tab string) string {
	return creditsURL(v.selectedID(), v.Query, v.State, tab)
}

func (v creditsView) CreditsTabHref() string { return v.tabHref("") }
func (v creditsView) AuditTabHref() string   { return v.tabHref("audit") }

func (v creditsView) selectedID() string {
	if v.Selected != nil {
		return v.Selected.ID
	}
	return v.Missing
}

func creditsURL(tc, q, state, tab string) string {
	values := url.Values{}
	for key, value := range map[string]string{"tc": tc, "q": q, "state": state, "tab": tab} {
		if value != "" {
			values.Set(key, value)
		}
	}
	if len(values) == 0 {
		return "/admin/time-credits"
	}
	return "/admin/time-credits?" + values.Encode()
}

func validState(state string) string {
	switch state {
	case "online", "paused", "expired":
		return state
	}
	return ""
}

func matchesQuery(view tcView, query string) bool {
	query = strings.ToLower(query)
	return strings.Contains(strings.ToLower(view.ID+" "+view.MAC+" "+view.IP+" "+view.State), query)
}

// creditsView builds the split view for the selected credit tc (empty selects
// the first listed one). withDetail also loads the timeline and Top-up key.
func (s *Server) creditsView(tc, query, state, tab string, withDetail bool) (creditsView, error) {
	rows, err := s.ledger.ListTimeCredits()
	if err != nil {
		return creditsView{}, err
	}
	snapshot := s.usageSnapshot()
	all := creditViews(rows, snapshot)
	view := creditsView{Query: query, State: validState(state), Tab: tab, Total: len(all), Known: snapshot.Known, Stale: snapshot.Stale}
	for _, credit := range all {
		if (view.State == "" || credit.State == view.State) && (query == "" || matchesQuery(credit, query)) {
			view.List = append(view.List, credit)
		}
	}
	if tc == "" && len(view.List) > 0 {
		tc = view.List[0].ID
	}
	for _, credit := range all {
		if tc != "" && credit.ID == tc {
			view.Selected = &credit
		}
	}
	if tc != "" && view.Selected == nil {
		view.Missing = tc
	}
	for i := range view.List {
		credit := &view.List[i]
		credit.Current = credit.ID == tc
		credit.Href = creditsURL(credit.ID, query, view.State, tab)
		// The detail response re-renders the list, so it needs the same filters.
		credit.DetailURL = "/admin/time-credits/detail?" + url.Values{"tc": {credit.ID}, "q": {query}, "state": {view.State}}.Encode()
	}
	review, err := s.ledger.AuditReview()
	if err != nil {
		return view, err
	}
	for _, item := range review {
		if item.Status == "pending-review" {
			view.Audit = append(view.Audit, item)
		}
	}
	if !withDetail || view.Selected == nil {
		return view, nil
	}
	history, err := s.ledger.TimeCreditHistory(view.Selected.ID)
	if err != nil {
		return view, err
	}
	for _, event := range history {
		view.Timeline = append(view.Timeline, timelineLine{Kind: event.Kind, Time: event.At.Format("Jan 2 15:04"), Text: event.Text})
	}
	key := make([]byte, 24)
	if _, err := rand.Read(key); err != nil {
		return view, err
	}
	view.RequestID = base64.RawURLEncoding.EncodeToString(key)
	return view, nil
}

// replyCredits writes a Time Credits template with the status that matches the view.
func replyCredits(response http.ResponseWriter, view creditsView, fragment string) {
	response.Header().Set("Content-Type", "text/html; charset=utf-8")
	if view.Missing != "" {
		response.WriteHeader(http.StatusNotFound)
	}
	renderFragment(response, "time_credits", fragment, view)
}

func (s *Server) timeCreditsPage(response http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	tab := ""
	if query.Get("tab") == "audit" {
		tab = "audit"
	}
	view, err := s.creditsView(query.Get("tc"), query.Get("q"), query.Get("state"), tab, true)
	if err != nil {
		http.Error(response, "read Time Credits", http.StatusInternalServerError)
		return
	}
	active := "/admin/time-credits"
	if view.State != "" {
		active += "?state=" + view.State
	}
	if view.Missing != "" {
		response.Header().Set("Content-Type", "text/html; charset=utf-8")
		response.WriteHeader(http.StatusNotFound)
	}
	s.renderPage(response, request, "time_credits", Page{Title: "Time Credits", Section: "Main", Active: active, Data: view})
}

// timeCreditsList is the search target: GET ?q=&state= returns the list items,
// highlighting the credit named by tc or, for htmx, the current browser URL.
func (s *Server) timeCreditsList(response http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	tc := query.Get("tc")
	if current, err := url.Parse(request.Header.Get("HX-Current-URL")); tc == "" && err == nil {
		tc = current.Query().Get("tc")
	}
	view, err := s.creditsView(tc, query.Get("q"), query.Get("state"), "", false)
	if err != nil {
		http.Error(response, "read Time Credits", http.StatusInternalServerError)
		return
	}
	// A search must not change the selection by itself; an unknown tc in the URL is not an error here.
	view.Missing = ""
	renderFragment(response, "time_credits", "list", view)
}

// timeCreditsDetail is the list-item click target: the detail pane plus an
// out-of-band list so the highlight follows the selection. Unknown tc is a 404
// carrying the same pane with a not-found message.
func (s *Server) timeCreditsDetail(response http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	view, err := s.creditsView(query.Get("tc"), query.Get("q"), query.Get("state"), "", true)
	if err != nil {
		http.Error(response, "read Time Credit", http.StatusInternalServerError)
		return
	}
	replyCredits(response, view, "detail_swap")
}

// search returns Time Credits as command-palette entries: a JSON array of
// searchItem the palette filters by label and hint (ID, MAC, IP, state).
func (s *Server) search(response http.ResponseWriter, _ *http.Request) {
	rows, err := s.ledger.ListTimeCredits()
	if err != nil {
		http.Error(response, "read Time Credits", http.StatusInternalServerError)
		return
	}
	items := []searchItem{}
	for _, credit := range creditViews(rows, s.usageSnapshot()) {
		items = append(items, searchItem{Label: "TC " + credit.ID, Hint: credit.Device() + " · " + credit.State, Href: credit.Href})
	}
	response.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(response).Encode(items); err != nil {
		return
	}
}
