package ledger

import (
	"database/sql"
	"errors"
	"time"
)

const validitySchema = `
INSERT INTO settings (key, value) VALUES ('validity_seconds', '2592000') ON CONFLICT(key) DO NOTHING;
CREATE TABLE IF NOT EXISTS time_credit_validity (
 time_credit_id TEXT PRIMARY KEY REFERENCES time_credits(id),
 valid_until INTEGER NOT NULL, expired_at INTEGER NOT NULL DEFAULT 0,
 router_removed INTEGER NOT NULL DEFAULT 0 CHECK(router_removed IN (0, 1))
);
INSERT INTO time_credit_validity (time_credit_id, valid_until)
 SELECT tc.id, COALESCE((SELECT unixepoch(MAX(at)) FROM audit WHERE time_credit_id = tc.id), unixepoch())
 + CAST((SELECT value FROM settings WHERE key = 'validity_seconds') AS INTEGER)
 FROM time_credits tc WHERE true ON CONFLICT(time_credit_id) DO NOTHING;
`

const MaxValiditySeconds int64 = 36500 * 86400

func (l *Ledger) ValiditySeconds() (int64, error) {
	var seconds int64
	err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'validity_seconds'").Scan(&seconds)
	return seconds, err
}

func (l *Ledger) SaveValiditySeconds(seconds int64) error {
	if seconds < 1 || seconds > MaxValiditySeconds {
		return errors.New("choose Validity of 1 second–36500 days")
	}
	_, err := l.db.Exec("UPDATE settings SET value = ? WHERE key = 'validity_seconds'", seconds)
	return err
}

type ValidityState struct {
	TimeCreditID  string `json:"time_credit_id"`
	ValidUntil    int64  `json:"valid_until"`
	ExpiredAt     int64  `json:"expired_at"`
	RouterRemoved bool   `json:"router_removed"`
}

func (v ValidityState) Expired() bool {
	return v.ExpiredAt != 0 || v.ValidUntil <= time.Now().Unix()
}

func (l *Ledger) Validity(id string) (ValidityState, error) {
	v := ValidityState{TimeCreditID: id}
	err := l.db.QueryRow("SELECT valid_until, expired_at, router_removed FROM time_credit_validity WHERE time_credit_id = ?", id).Scan(&v.ValidUntil, &v.ExpiredAt, &v.RouterRemoved)
	return v, err
}

func (l *Ledger) Validities() ([]ValidityState, error) {
	rows, err := l.db.Query("SELECT time_credit_id, valid_until, expired_at, router_removed FROM time_credit_validity ORDER BY time_credit_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	states := []ValidityState{}
	for rows.Next() {
		var v ValidityState
		if err := rows.Scan(&v.TimeCreditID, &v.ValidUntil, &v.ExpiredAt, &v.RouterRemoved); err != nil {
			return nil, err
		}
		states = append(states, v)
	}
	return states, rows.Err()
}

// ExpireTimeCredits re-reads every deadline inside the write transaction, not
// from a Router active-list snapshot. A Top-up committed first wins. Once this
// retirement commits, a later purchase must create a new Time Credit.
func (l *Ledger) ExpireTimeCredits() error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	rows, err := tx.Query(`SELECT tc.id, tc.bought_seconds FROM time_credits tc
 JOIN time_credit_validity v ON v.time_credit_id = tc.id WHERE v.expired_at = 0 AND v.valid_until <= ?`, now)
	if err != nil {
		return err
	}
	type expired struct {
		id     string
		bought int64
	}
	timeCredits := []expired{}
	for rows.Next() {
		var tc expired
		if err := rows.Scan(&tc.id, &tc.bought); err != nil {
			rows.Close()
			return err
		}
		timeCredits = append(timeCredits, tc)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, tc := range timeCredits {
		if _, err := tx.Exec(`UPDATE time_credit_validity SET expired_at = ? WHERE time_credit_id = ? AND expired_at = 0 AND valid_until <= ?`, now, tc.id, now); err != nil {
			return err
		}
		// Bought time remains historical. The retirement forfeits access, not a
		// guessed used/remaining-time quantity (only the Router meters that).
		if _, err := tx.Exec(`INSERT INTO audit (at, actor, reason, time_credit_id, before_seconds, after_seconds, source)
 VALUES (?, 'system', 'validity-expiry', ?, ?, 0, ?)`, time.Unix(now, 0).UTC().Format(time.RFC3339), tc.id, tc.bought, "validity:"+tc.id); err != nil {
			return err
		}
		// Supersede any interrupted credit projection so it cannot recreate the
		// retired Router user. Removal is retried until router_removed is set.
		if _, err := tx.Exec(`UPDATE credit_intents SET applied_revision = revision, error = '' WHERE time_credit_id = ?`, tc.id); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (l *Ledger) FinishValidityRemoval(id string) error {
	_, err := l.db.Exec("UPDATE time_credit_validity SET router_removed = 1 WHERE time_credit_id = ? AND expired_at != 0 AND router_removed = 0", id)
	return err
}

var ErrTimeCreditExpired = errors.New("Validity has ended. Unused time is forfeited. Enter another Voucher for a new Time Credit")

// siteValiditySeconds is the Validity of Vouchers and admin grants. It reads
// inside the caller's transaction: the ledger has a single connection.
func siteValiditySeconds(tx *sql.Tx) (int64, error) {
	var seconds int64
	err := tx.QueryRow("SELECT value FROM settings WHERE key = 'validity_seconds'").Scan(&seconds)
	return seconds, err
}

// ExtendValidity belongs in the same transaction as each purchase, Pause reset,
// audit and credit intent. A Top-up never shortens Validity (ADR 0006): the
// deadline becomes the later of the current one and now + seconds. A Time
// Credit with no bought time only holds a reservation's placeholder deadline,
// so its first Top-up sets the deadline outright. Time Credits past their
// deadline cannot be revived by a Top-up.
func ExtendValidity(tx *sql.Tx, id string, seconds int64) error {
	now := time.Now().Unix()
	until := now + seconds
	result, err := tx.Exec(`INSERT INTO time_credit_validity (time_credit_id, valid_until) VALUES (?, ?)
 ON CONFLICT(time_credit_id) DO UPDATE SET valid_until = CASE
  WHEN (SELECT bought_seconds FROM time_credits WHERE id = time_credit_validity.time_credit_id) = 0 THEN excluded.valid_until
  ELSE max(valid_until, excluded.valid_until) END
 WHERE expired_at = 0 AND valid_until > ?`, id, until, now)
	if err != nil {
		return err
	}
	changed, err := result.RowsAffected()
	if err == nil && changed == 0 {
		return ErrTimeCreditExpired
	}
	return err
}
