package ledger

import (
	"fmt"
	"sort"
	"time"
)

// TimeCreditRow is the admin list read model: ledger facts only. Used and
// remaining time are never here; only the Router meters them.
type TimeCreditRow struct {
	ID            string
	MAC           string
	IP            string
	BoughtSeconds int64
	PointsMilli   int64
	ValidUntil    int64 // unix seconds
	ExpiredAt     int64 // unix seconds, 0 while valid
	Paused        bool  // the customer tapped Pause
}

// Expired reports whether Validity has ended, by the retirement record or the deadline.
func (r TimeCreditRow) Expired() bool {
	return r.ExpiredAt != 0 || r.ValidUntil <= time.Now().Unix()
}

// ListTimeCredits reads every Time Credit with its Validity and Pause state in
// one query, without the vouchers, audit and rebind history PurchaseState loads.
func (l *Ledger) ListTimeCredits() ([]TimeCreditRow, error) {
	rows, err := l.db.Query(`SELECT tc.id, tc.mac, tc.ip, tc.bought_seconds, tc.points_milli,
		COALESCE(v.valid_until, 0), COALESCE(v.expired_at, 0), COALESCE(p.tapped, 0)
		FROM time_credits tc
		LEFT JOIN time_credit_validity v ON v.time_credit_id = tc.id
		LEFT JOIN time_credit_pauses p ON p.time_credit_id = tc.id
		ORDER BY tc.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	credits := []TimeCreditRow{}
	for rows.Next() {
		var row TimeCreditRow
		if err := rows.Scan(&row.ID, &row.MAC, &row.IP, &row.BoughtSeconds, &row.PointsMilli, &row.ValidUntil, &row.ExpiredAt, &row.Paused); err != nil {
			return nil, err
		}
		credits = append(credits, row)
	}
	return credits, rows.Err()
}

// HistoryEvent is one line of a Time Credit's timeline.
type HistoryEvent struct {
	At   time.Time
	Kind string // topup, expiry, rebind or coin
	Text string
}

// TimeCreditHistory merges a credit's audit rows (purchases, grants, expiry),
// Rebinds and Coin Windows, newest first.
func (l *Ledger) TimeCreditHistory(id string) ([]HistoryEvent, error) {
	events := []HistoryEvent{}
	audits, err := l.db.Query("SELECT at, reason, before_seconds, after_seconds FROM audit WHERE time_credit_id = ?", id)
	if err != nil {
		return nil, err
	}
	for audits.Next() {
		var at, reason string
		var before, after int64
		if err := audits.Scan(&at, &reason, &before, &after); err != nil {
			audits.Close()
			return nil, err
		}
		kind := "topup"
		if reason == "validity-expiry" {
			kind = "expiry"
		}
		events = append(events, HistoryEvent{parseTime(at), kind, fmt.Sprintf("%s · bought %ds → %ds", reason, before, after)})
	}
	err = audits.Err()
	audits.Close()
	if err != nil {
		return nil, err
	}
	rebinds, err := l.db.Query("SELECT at, old_mac, old_ip, new_mac, new_ip, via FROM rebinds WHERE time_credit_id = ?", id)
	if err != nil {
		return nil, err
	}
	for rebinds.Next() {
		var at, oldMAC, oldIP, newMAC, newIP, via string
		if err := rebinds.Scan(&at, &oldMAC, &oldIP, &newMAC, &newIP, &via); err != nil {
			rebinds.Close()
			return nil, err
		}
		events = append(events, HistoryEvent{parseTime(at), "rebind", fmt.Sprintf("Rebind (%s) · %s → %s", via, device(oldMAC, oldIP), device(newMAC, newIP))})
	}
	err = rebinds.Err()
	rebinds.Close()
	if err != nil {
		return nil, err
	}
	windows, err := l.db.Query("SELECT id, status, coins, credited_seconds, opened_at FROM coin_windows WHERE time_credit_id = ?", id)
	if err != nil {
		return nil, err
	}
	defer windows.Close()
	for windows.Next() {
		var windowID, coins, credited, opened int64
		var status string
		if err := windows.Scan(&windowID, &status, &coins, &credited, &opened); err != nil {
			return nil, err
		}
		events = append(events, HistoryEvent{time.UnixMilli(opened).UTC(), "coin", fmt.Sprintf("Coin Window #%d (%s) · %d coins · %ds credited", windowID, status, coins, credited)})
	}
	if err := windows.Err(); err != nil {
		return nil, err
	}
	sort.SliceStable(events, func(i, j int) bool { return events[i].At.After(events[j].At) })
	return events, nil
}

func parseTime(value string) time.Time {
	at, _ := time.Parse(time.RFC3339, value)
	return at
}

func device(mac, ip string) string {
	if mac == "" && ip == "" {
		return "unbound"
	}
	return mac + " " + ip
}
