package ledger

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// Activity kinds, also the Logs page filter values.
const (
	ActivityCoin    = "coin"
	ActivityVoucher = "voucher"
	ActivityRebind  = "rebind"
	ActivityGrant   = "grant"
	ActivityAudit   = "audit"
)

// ActivityEvent is one line of the Logs feed, derived from an append-only table.
// Pauses are absent: the ledger keeps only the current Pause state, no history.
type ActivityEvent struct {
	Key          string // source table + row id, unique and stable across polls
	At           time.Time
	Kind         string
	Title        string
	Detail       string
	TimeCreditID string // empty when the event has no Time Credit (Vendo audit flags)
}

// Activity merges the event sources. kind "" means every kind. Only events not
// before `since` are returned (zero time: no lower bound), at most limit; the
// bound is inclusive because audit and Rebind times have one-second resolution,
// so callers dedupe by Key. Newest first by default; oldestFirst returns the
// oldest `limit` events after `since`, which lets a poller page forward without
// skipping any. The kind filter and the bound are in each source's SQL, ahead of
// its LIMIT, so a rare kind is not crowded out by newer rows of another kind.
// Newest-first reads walk the primary key and need no extra index; oldest-first
// reads order by event time and scan their table.
func (l *Ledger) Activity(kind string, since time.Time, limit int, oldestFirst bool) ([]ActivityEvent, error) {
	query := activityQuery{kind: kind, since: since.Unix(), limit: limit, oldestFirst: oldestFirst}
	var events []ActivityEvent
	for _, source := range []struct {
		kinds []string
		read  func(activityQuery) ([]ActivityEvent, error)
	}{
		{[]string{ActivityVoucher, ActivityGrant, ActivityAudit}, l.auditActivity},
		{[]string{ActivityRebind}, l.rebindActivity},
		{[]string{ActivityCoin}, l.coinActivity},
		{[]string{ActivityAudit}, l.vendoAuditActivity},
		{[]string{ActivityAudit}, l.voucherDeletionActivity},
	} {
		if kind != "" && !contains(source.kinds, kind) {
			continue
		}
		found, err := source.read(query)
		if err != nil {
			return nil, err
		}
		for _, event := range found {
			if (kind == "" || event.Kind == kind) && !event.At.Before(since) {
				events = append(events, event)
			}
		}
	}
	sort.SliceStable(events, func(i, j int) bool {
		if oldestFirst {
			return events[i].At.Before(events[j].At)
		}
		return events[i].At.After(events[j].At)
	})
	if len(events) > limit {
		events = events[:limit]
	}
	return events, nil
}

// activityQuery is what a source needs to build its WHERE and ORDER BY.
type activityQuery struct {
	kind        string
	since       int64 // unix seconds, inclusive
	limit       int
	oldestFirst bool
}

// order returns the ORDER BY clause: by event time when paging forward, else by id.
func (q activityQuery) order(seconds, id string) string {
	if q.oldestFirst {
		return "ORDER BY " + seconds + " ASC, " + id + " ASC"
	}
	return "ORDER BY " + id + " DESC"
}

// textSeconds is the unix-seconds SQL for an RFC3339 text column.
func textSeconds(column string) string {
	return "CAST(strftime('%s', " + column + ") AS INTEGER)"
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

// auditActivity reads the Time Credit audit by reason: voucher redemptions,
// admin grants, and validity expiries. coin-top-up rows are skipped because the
// settled Coin Window already reports them with the coin count.
func (l *Ledger) auditActivity(q activityQuery) ([]ActivityEvent, error) {
	// The kind is derived from the reason in the switch below; mirror it here.
	kindWhere := ""
	switch q.kind {
	case ActivityVoucher:
		kindWhere = " AND reason = 'voucher-redemption'"
	case ActivityGrant:
		kindWhere = " AND substr(reason, 1, 11) = 'admin-grant'"
	case ActivityAudit:
		kindWhere = " AND reason != 'voucher-redemption' AND substr(reason, 1, 11) != 'admin-grant'"
	}
	rows, err := l.db.Query(`SELECT id, at, actor, reason, time_credit_id, before_seconds, after_seconds, source
		FROM audit WHERE reason != 'coin-top-up' AND `+textSeconds("at")+` >= ?`+kindWhere+` `+q.order(textSeconds("at"), "id")+` LIMIT ?`, q.since, q.limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []ActivityEvent
	for rows.Next() {
		var id, before, afterSeconds int64
		var at, actor, reason, timeCreditID, source string
		if err := rows.Scan(&id, &at, &actor, &reason, &timeCreditID, &before, &afterSeconds, &source); err != nil {
			return nil, err
		}
		event := ActivityEvent{Key: fmt.Sprintf("audit-%d", id), At: parseActivityTime(at), TimeCreditID: timeCreditID}
		added := formatActivitySeconds(afterSeconds - before)
		switch {
		case reason == "voucher-redemption":
			event.Kind, event.Title = ActivityVoucher, "Voucher redeemed"
			event.Detail = fmt.Sprintf("Code %s by %s, +%s", source, actor, added)
		case strings.HasPrefix(reason, "admin-grant"):
			event.Kind, event.Title = ActivityGrant, "Time granted"
			event.Detail = fmt.Sprintf("+%s by %s: %s", added, actor, strings.TrimSpace(strings.TrimPrefix(reason, "admin-grant:")))
		default:
			event.Kind, event.Title = ActivityAudit, humanizeReason(reason)
			event.Detail = "By " + actor
		}
		events = append(events, event)
	}
	return events, rows.Err()
}

func (l *Ledger) rebindActivity(q activityQuery) ([]ActivityEvent, error) {
	rows, err := l.db.Query(`SELECT id, at, time_credit_id, old_mac, new_mac, via FROM rebinds WHERE `+textSeconds("at")+` >= ? `+
		q.order(textSeconds("at"), "id")+` LIMIT ?`, q.since, q.limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []ActivityEvent
	for rows.Next() {
		var id int64
		var at, timeCreditID, oldMAC, newMAC, via string
		if err := rows.Scan(&id, &at, &timeCreditID, &oldMAC, &newMAC, &via); err != nil {
			return nil, err
		}
		events = append(events, ActivityEvent{
			Key: fmt.Sprintf("rebind-%d", id), At: parseActivityTime(at), Kind: ActivityRebind, Title: "Rebind",
			Detail:       fmt.Sprintf("%s to %s via %s", oldMAC, newMAC, via),
			TimeCreditID: timeCreditID,
		})
	}
	return events, rows.Err()
}

// coinActivity reports one event per settled Coin Window that took coins. Windows
// with zero coins are open/close noise and skipped. The time is the close time,
// falling back to the last coin for windows settled without a recorded close.
func (l *Ledger) coinActivity(q activityQuery) ([]ActivityEvent, error) {
	const closedAt = "MAX(closed_at, close_at, last_coin_at)" // milliseconds
	rows, err := l.db.Query(`SELECT id, vendo_id, time_credit_id, coins, credited_seconds, `+closedAt+`
		FROM coin_windows WHERE status = 'settled' AND coins > 0 AND `+closedAt+` >= ? `+q.order(closedAt, "id")+` LIMIT ?`, q.since*1000, q.limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []ActivityEvent
	for rows.Next() {
		var id, vendoID, coins, credited, closedAt int64
		var timeCreditID string
		if err := rows.Scan(&id, &vendoID, &timeCreditID, &coins, &credited, &closedAt); err != nil {
			return nil, err
		}
		events = append(events, ActivityEvent{
			Key: fmt.Sprintf("window-%d", id), At: time.UnixMilli(closedAt), Kind: ActivityCoin, Title: "Coin Window settled",
			Detail:       fmt.Sprintf("%d %s on Vendo #%d, +%s", coins, plural(coins, "coin"), vendoID, formatActivitySeconds(credited)),
			TimeCreditID: timeCreditID,
		})
	}
	return events, rows.Err()
}

// vendoAuditActivity reports Vendo audit flags and their resolutions.
func (l *Ledger) vendoAuditActivity(q activityQuery) ([]ActivityEvent, error) {
	var events []ActivityEvent
	flags, err := l.db.Query(`SELECT id, vendo_id, reason, coins, recorded_at FROM vendo_audit WHERE `+textSeconds("recorded_at")+` >= ? `+
		q.order(textSeconds("recorded_at"), "id")+` LIMIT ?`, q.since, q.limit)
	if err != nil {
		return nil, err
	}
	defer flags.Close()
	for flags.Next() {
		var id, vendoID, coins int64
		var reason, at string
		if err := flags.Scan(&id, &vendoID, &reason, &coins, &at); err != nil {
			return nil, err
		}
		detail := fmt.Sprintf("Vendo #%d", vendoID)
		if coins > 0 {
			detail += fmt.Sprintf(", %d %s", coins, plural(coins, "coin"))
		}
		events = append(events, ActivityEvent{
			Key: fmt.Sprintf("vendo-audit-%d", id), At: parseActivityTime(at), Kind: ActivityAudit,
			Title: "Audit flag: " + humanizeReason(reason), Detail: detail,
		})
	}
	if err := flags.Err(); err != nil {
		return nil, err
	}
	resolved, err := l.db.Query(`SELECT audit_id, status, resolved_at, reason FROM audit_resolutions WHERE `+textSeconds("resolved_at")+` >= ? `+
		q.order(textSeconds("resolved_at"), "audit_id")+` LIMIT ?`, q.since, q.limit)
	if err != nil {
		return nil, err
	}
	defer resolved.Close()
	for resolved.Next() {
		var auditID int64
		var status, at, reason string
		if err := resolved.Scan(&auditID, &status, &at, &reason); err != nil {
			return nil, err
		}
		title := "Audit flag dismissed"
		if status == "granted" {
			title = "Audit flag resolved with a grant"
		}
		events = append(events, ActivityEvent{
			Key: fmt.Sprintf("resolution-%d", auditID), At: parseActivityTime(at), Kind: ActivityAudit,
			Title: title, Detail: fmt.Sprintf("Flag #%d: %s", auditID, reason),
		})
	}
	return events, resolved.Err()
}

// parseActivityTime reads the RFC3339 / RFC3339Nano text the ledger writes. A
// malformed value yields the zero time, which sorts last.
func parseActivityTime(value string) time.Time {
	at, _ := time.Parse(time.RFC3339Nano, value)
	return at
}

func humanizeReason(reason string) string {
	reason = strings.NewReplacer("_", " ", "-", " ").Replace(reason)
	if reason == "" {
		return reason
	}
	return strings.ToUpper(reason[:1]) + reason[1:]
}

func plural(n int64, word string) string {
	if n == 1 {
		return word
	}
	return word + "s"
}

// formatActivitySeconds renders a duration like "1h 5m", "30m" or "45s".
func formatActivitySeconds(seconds int64) string {
	if seconds < 60 {
		return fmt.Sprintf("%ds", seconds)
	}
	hours, minutes := seconds/3600, seconds%3600/60
	switch {
	case hours == 0:
		return fmt.Sprintf("%dm", minutes)
	case minutes == 0:
		return fmt.Sprintf("%dh", hours)
	}
	return fmt.Sprintf("%dh %dm", hours, minutes)
}
