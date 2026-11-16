package ledger

import (
	"database/sql"
	"errors"
	"time"
)

// Resume Codes and Rebinds live in their own tables so the Time Credit row
// keeps only its current Device binding.
const rebindSchema = `
CREATE TABLE IF NOT EXISTS resume_codes (
 time_credit_id TEXT PRIMARY KEY REFERENCES time_credits(id), code TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS rebinds (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, time_credit_id TEXT NOT NULL REFERENCES time_credits(id),
 old_mac TEXT NOT NULL, old_ip TEXT NOT NULL, new_mac TEXT NOT NULL, new_ip TEXT NOT NULL, via TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS rebinds_no_update BEFORE UPDATE ON rebinds BEGIN SELECT RAISE(ABORT, 'append-only rebinds'); END;
CREATE TRIGGER IF NOT EXISTS rebinds_no_delete BEFORE DELETE ON rebinds BEGIN SELECT RAISE(ABORT, 'append-only rebinds'); END;
`

// insertResumeCode gives a Time Credit its permanent Resume Code, retrying on
// the rare collision with another Time Credit's code.
func insertResumeCode(tx *sql.Tx, timeCreditID string) error {
	for {
		code, err := randomCode()
		if err != nil {
			return err
		}
		result, err := tx.Exec("INSERT INTO resume_codes (time_credit_id, code) VALUES (?, ?) ON CONFLICT(code) DO NOTHING", timeCreditID, code)
		if err != nil {
			return err
		}
		inserted, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if inserted == 1 {
			return nil
		}
	}
}

// backfillResumeCodes runs on open so Time Credits bought before Resume Codes
// existed get one too.
func (l *Ledger) backfillResumeCodes() error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	rows, err := tx.Query("SELECT id FROM time_credits WHERE id NOT IN (SELECT time_credit_id FROM resume_codes)")
	if err != nil {
		return err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if err := insertResumeCode(tx, id); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// ResumeCode is a credential: show it only to the Device the Time Credit is
// bound to, never in admin readback.
func (l *Ledger) ResumeCode(timeCreditID string) (string, error) {
	var code string
	err := l.db.QueryRow("SELECT code FROM resume_codes WHERE time_credit_id = ?", timeCreditID).Scan(&code)
	return code, err
}

func (l *Ledger) TimeCreditByResumeCode(code string) (TimeCredit, error) {
	return scanTimeCredit(l.db.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = (SELECT time_credit_id FROM resume_codes WHERE code = ?)", code))
}

type Rebind struct {
	ID           int64  `json:"id"`
	At           string `json:"at"`
	TimeCreditID string `json:"time_credit_id"`
	OldMAC       string `json:"old_mac"`
	OldIP        string `json:"old_ip"`
	NewMAC       string `json:"new_mac"`
	NewIP        string `json:"new_ip"`
	Via          string `json:"via"`
}

var ErrBindingChanged = errors.New("Time Credit moved to another Device; try again")

func (l *Ledger) DeviceTimeCreditIDs(mac, ip string) ([]string, error) {
	rows, err := l.db.Query("SELECT id FROM time_credits WHERE mac = ? AND ip = ?", mac, ip)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// Rebind moves a Time Credit and unbinds any displaced Time Credit atomically.
// Empty MAC/IP means unbound: time and recovery credentials are retained. Callers
// remove old/displaced Router sessions first. via is cookie or resume-code.
func (l *Ledger) Rebind(id, oldMAC, oldIP, newMAC, newIP, via string) (TimeCredit, error) {
	tx, err := l.db.Begin()
	if err != nil {
		return TimeCredit{}, err
	}
	defer tx.Rollback()
	now := time.Now().UTC().Format(time.RFC3339)
	if _, err := tx.Exec(`INSERT INTO rebinds (at, time_credit_id, old_mac, old_ip, new_mac, new_ip, via)
		SELECT ?, id, mac, ip, '', '', 'displaced' FROM time_credits WHERE mac = ? AND ip = ? AND id != ?`, now, newMAC, newIP, id); err != nil {
		return TimeCredit{}, err
	}
	if _, err := tx.Exec("UPDATE time_credits SET mac = '', ip = '' WHERE mac = ? AND ip = ? AND id != ?", newMAC, newIP, id); err != nil {
		return TimeCredit{}, err
	}
	result, err := tx.Exec("UPDATE time_credits SET mac = ?, ip = ? WHERE id = ? AND mac = ? AND ip = ?", newMAC, newIP, id, oldMAC, oldIP)
	if err != nil {
		return TimeCredit{}, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return TimeCredit{}, err
	}
	if changed != 1 {
		return TimeCredit{}, ErrBindingChanged
	}
	if oldMAC != newMAC || oldIP != newIP {
		_, err = tx.Exec("INSERT INTO rebinds (at, time_credit_id, old_mac, old_ip, new_mac, new_ip, via) VALUES (?, ?, ?, ?, ?, ?, ?)",
			now, id, oldMAC, oldIP, newMAC, newIP, via)
		if err != nil {
			return TimeCredit{}, err
		}
	}
	tc, err := scanTimeCredit(tx.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = ?", id))
	if err != nil {
		return tc, err
	}
	// Supersede interrupted projection work with the newest Device binding.
	// An unpaid Coin Window reservation must never project RouterOS's zero
	// (unlimited) limit; its first coin Top-up will queue the paid target.
	// An expired Time Credit Rebinds only to keep its Points; its forfeited
	// time is never projected again.
	var live bool
	if err := tx.QueryRow("SELECT expired_at = 0 AND valid_until > unixepoch() FROM time_credit_validity WHERE time_credit_id = ?", id).Scan(&live); err != nil {
		return tc, err
	}
	if tc.BoughtSeconds > 0 && live {
		if err := QueueCreditIntent(tx, tc.ID, tc.BoughtSeconds); err != nil {
			return tc, err
		}
	}
	return tc, tx.Commit()
}

func (l *Ledger) rebinds() ([]Rebind, error) {
	rows, err := l.db.Query("SELECT id, at, time_credit_id, old_mac, old_ip, new_mac, new_ip, via FROM rebinds ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	rebinds := []Rebind{}
	for rows.Next() {
		var r Rebind
		if err := rows.Scan(&r.ID, &r.At, &r.TimeCreditID, &r.OldMAC, &r.OldIP, &r.NewMAC, &r.NewIP, &r.Via); err != nil {
			return nil, err
		}
		rebinds = append(rebinds, r)
	}
	return rebinds, rows.Err()
}
