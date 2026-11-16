package controller

import (
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

const logsLimit = 100

type logFilter struct {
	Kind, Label string
}

var logFilters = []logFilter{
	{"", "All"}, {ledger.ActivityCoin, "Coins"}, {ledger.ActivityVoucher, "Vouchers"},
	{ledger.ActivityRebind, "Rebinds"}, {ledger.ActivityGrant, "Grants"}, {ledger.ActivityAudit, "Audit"},
}

// logEvent is one rendered feed item.
type logEvent struct {
	ledger.ActivityEvent
	Clock string // local time, with the date when not today
	Stamp string // RFC3339 for the <time> element
}

// logsView is the dot of the "content" and "feed" templates.
type logsView struct {
	Kind    string
	Filters []logFilter
	Events  []logEvent
	Cursor  string // see feedCursor
}

// logsPage renders the Logs page: the newest events of the chosen kind and a
// poller whose cursor starts at the newest event of any kind.
func (s *Server) logsPage(response http.ResponseWriter, request *http.Request) {
	kind := logKind(request.URL.Query().Get("kind"))
	all, err := s.ledger.Activity("", time.Time{}, logsLimit, false)
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	events := all
	if kind != "" {
		if events, err = s.ledger.Activity(kind, time.Time{}, logsLimit, false); err != nil {
			http.Error(response, err.Error(), http.StatusInternalServerError)
			return
		}
	}
	cursor, keys := feedCursor(all, time.Time{}, nil)
	// An event that landed between the two reads is newer than the cursor, so the
	// first poll delivers it; leave it out here to avoid showing it twice.
	if len(all) > 0 {
		for len(events) > 0 && events[0].At.After(all[0].At) {
			events = events[1:]
		}
	}
	view := logsView{Kind: kind, Filters: logFilters, Events: logEvents(events), Cursor: formatCursor(cursor, keys)}
	s.renderPage(response, request, "logs", Page{Title: "Logs", Section: "Main", Active: "/admin/logs", Data: view})
}

// logsFeed answers the 5s poll with only the events newer than the cursor, for
// the active filter, to be prepended to the list. It replies with an empty 200
// when nothing is new.
//
// The poll reads the oldest logsLimit events after the cursor, so a burst larger
// than that is delivered over successive polls and the cursor never moves past
// an event that was not sent. The cursor is the newest event time delivered (of
// ANY kind, so it advances past events the filter hides) plus the keys of the
// events at exactly that time:
// audit and Rebind times have one-second resolution, so a later event can share
// the cursor's second and must still show up once. The poller reads the cursor
// from a hidden input, which every non-empty response replaces out-of-band.
func (s *Server) logsFeed(response http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	kind := logKind(query.Get("kind"))
	at, seen, ok := parseCursor(query.Get("after"))
	if !ok {
		return
	}
	all, err := s.ledger.Activity("", at, logsLimit, true)
	if err != nil {
		http.Error(response, err.Error(), http.StatusInternalServerError)
		return
	}
	var fresh, shown []ledger.ActivityEvent
	for _, event := range all {
		if event.At.Equal(at) && seen[event.Key] {
			continue
		}
		fresh = append(fresh, event)
		if kind == "" || event.Kind == kind {
			shown = append(shown, event)
		}
	}
	if len(fresh) == 0 {
		return
	}
	// Oldest-first reads are newest-first on screen, and feedCursor wants newest first.
	slices.Reverse(fresh)
	slices.Reverse(shown)
	next, keys := feedCursor(fresh, at, seen)
	renderFragment(response, "logs", "feed", logsView{Kind: kind, Events: logEvents(shown), Cursor: formatCursor(next, keys)})
}

func logKind(kind string) string {
	for _, filter := range logFilters {
		if filter.Kind == kind {
			return kind
		}
	}
	return ""
}

// feedCursor returns the newest time among events (newest first) and the keys
// at that time. When that time equals the previous cursor time, the previous
// keys carry over.
func feedCursor(events []ledger.ActivityEvent, previous time.Time, seen map[string]bool) (time.Time, map[string]bool) {
	if len(events) == 0 {
		return previous, seen
	}
	at, keys := events[0].At, map[string]bool{}
	for _, event := range events {
		if event.At.Equal(at) {
			keys[event.Key] = true
		}
	}
	if at.Equal(previous) {
		for key := range seen {
			keys[key] = true
		}
	}
	return at, keys
}

// Cursor wire format: "<unix nanoseconds>" then ",<key>" per key at that time.
// Keys are [a-z0-9-] only, so the value needs no escaping beyond the query's.
func formatCursor(at time.Time, keys map[string]bool) string {
	var out strings.Builder
	nanos := int64(0) // an empty ledger: the zero time has no UnixNano
	if !at.IsZero() {
		nanos = at.UnixNano()
	}
	out.WriteString(strconv.FormatInt(nanos, 10))
	for key := range keys {
		out.WriteString("," + key)
	}
	return out.String()
}

func parseCursor(value string) (time.Time, map[string]bool, bool) {
	parts := strings.Split(value, ",")
	nanos, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return time.Time{}, nil, false
	}
	seen := map[string]bool{}
	for _, key := range parts[1:] {
		seen[key] = true
	}
	return time.Unix(0, nanos), seen, true
}

func logEvents(events []ledger.ActivityEvent) []logEvent {
	now := time.Now()
	out := make([]logEvent, len(events))
	for i, event := range events {
		local := event.At.Local()
		layout := "Jan 2, 15:04"
		if local.YearDay() == now.YearDay() && local.Year() == now.Year() {
			layout = "15:04:05"
		}
		out[i] = logEvent{ActivityEvent: event, Clock: local.Format(layout), Stamp: local.Format(time.RFC3339)}
	}
	return out
}
