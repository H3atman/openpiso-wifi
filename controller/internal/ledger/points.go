package ledger

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// Points (ADR 0006) are a balance on the Time Credit, kept in thousandths so
// the ledger never stores floats. Every change has a journal row written in
// the same transaction as the Top-up that caused it.
const pointsSchema = `
CREATE TABLE IF NOT EXISTS points_journal (
 id INTEGER PRIMARY KEY, time_credit_id TEXT NOT NULL REFERENCES time_credits(id),
 delta_milli INTEGER NOT NULL CHECK(delta_milli != 0), reason TEXT NOT NULL, ref TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS points_journal_no_update BEFORE UPDATE ON points_journal BEGIN SELECT RAISE(ABORT, 'append-only points journal'); END;
CREATE TRIGGER IF NOT EXISTS points_journal_no_delete BEFORE DELETE ON points_journal BEGIN SELECT RAISE(ABORT, 'append-only points journal'); END;
`

// PointsEntry is one journal row: coin-earn (ref coin-window:<id>:<coins>),
// carry-out/carry-in (ref the other Time Credit), or redeem (ref
// points-row:<points_milli>:<seconds>).
type PointsEntry struct {
	ID           int64  `json:"id"`
	TimeCreditID string `json:"time_credit_id"`
	DeltaMilli   int64  `json:"delta_milli"`
	Reason       string `json:"reason"`
	Ref          string `json:"ref"`
	At           string `json:"at"`
}

// migratePoints adds the balance to Time Credits and, to Coin Windows, the
// Points already earned. coin_windows.carry_from is no longer used: Points now
// move when the window opens; the column stays for databases that have it.
func (l *Ledger) migratePoints() error {
	for _, column := range []struct{ table, name, definition string }{
		{"time_credits", "points_milli", "INTEGER NOT NULL DEFAULT 0 CHECK(points_milli >= 0)"},
		{"coin_windows", "points_milli", "INTEGER NOT NULL DEFAULT 0"},
		{"coin_windows", "carry_from", "TEXT REFERENCES time_credits(id)"},
	} {
		var count int
		if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info(?) WHERE name = ?", column.table, column.name).Scan(&count); err != nil {
			return err
		}
		if count == 0 {
			if _, err := l.db.Exec("ALTER TABLE " + column.table + " ADD COLUMN " + column.name + " " + column.definition); err != nil {
				return err
			}
		}
	}
	_, err := l.db.Exec(pointsSchema + `INSERT OR IGNORE INTO settings (key, value) VALUES ('points_tiers', '[]');`)
	return err
}

// PointsTier is one row of a Points table: PointsMilli buys a Top-up of
// Seconds with this Validity and Pause Limit (ADR 0006).
type PointsTier struct {
	PointsMilli     int64 `json:"points_milli"`
	Seconds         int64 `json:"seconds"`
	ValiditySeconds int64 `json:"validity_seconds"`
	PauseLimit      int   `json:"pause_limit"`
}

// PointsTiers is the site Points table; empty until the Operator sets one.
func (l *Ledger) PointsTiers() ([]PointsTier, error) {
	var data []byte
	if err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'points_tiers'").Scan(&data); err != nil {
		return nil, err
	}
	var tiers []PointsTier
	err := json.Unmarshal(data, &tiers)
	return tiers, err
}

func (l *Ledger) SavePointsTiers(tiers []PointsTier) error {
	data, err := pointsTiersJSON(tiers)
	if err != nil {
		return err
	}
	_, err = l.db.Exec("UPDATE settings SET value = ? WHERE key = 'points_tiers'", data)
	return err
}

// CustomizeVendoPoints gives a Vendo its own copy of the site Points table, to
// edit with SaveVendoPointsTiers.
func (l *Ledger) CustomizeVendoPoints(id uint32) error {
	_, err := l.db.Exec("UPDATE vendos SET points_tiers = (SELECT value FROM settings WHERE key = 'points_tiers') WHERE id = ?", id)
	return err
}

// SaveVendoPointsTiers sets a Vendo's own Points table, under the site table's
// rules; an empty table offers no redemptions on the Vendo's HotSpot server.
func (l *Ledger) SaveVendoPointsTiers(id uint32, tiers []PointsTier) error {
	data, err := pointsTiersJSON(tiers)
	if err != nil {
		return err
	}
	_, err = l.db.Exec("UPDATE vendos SET points_tiers = ? WHERE id = ?", data, id)
	return err
}

// UseSitePoints drops a Vendo's own Points table; its Devices redeem from the site's.
func (l *Ledger) UseSitePoints(id uint32) error {
	_, err := l.db.Exec("UPDATE vendos SET points_tiers = NULL WHERE id = ?", id)
	return err
}

// pointsTiersJSON validates a Points table, site or Vendo, and encodes it for storage.
func pointsTiersJSON(tiers []PointsTier) ([]byte, error) {
	if len(tiers) > 100 {
		return nil, errors.New("choose up to 100 Points rows")
	}
	for _, tier := range tiers {
		if tier.PointsMilli < 1 || tier.PointsMilli > 1000000*1000 || tier.Seconds < 1 || tier.Seconds > 31536000 {
			return nil, errors.New("choose 0.001–1000000 Points and 1–31536000 seconds")
		}
		if tier.ValiditySeconds < 1 || tier.ValiditySeconds > MaxValiditySeconds || tier.PauseLimit < 0 || tier.PauseLimit > 1000 {
			return nil, errors.New("choose a Validity of 1 second–36500 days (or No limit) and a Pause Limit of 0–1000")
		}
	}
	if tiers == nil {
		tiers = []PointsTier{} // stored as [], never null, so a customized empty table stays customized
	}
	return json.Marshal(tiers)
}

var ErrPointsRowUnknown = errors.New("That Points row is no longer offered. Choose again")
var ErrPointsTooLow = errors.New("not enough Points")

// RedeemPoints buys the row of table (the Points table offered to the Device:
// its serving Vendo's or the site's) costing pointsMilli for seconds, as a
// Top-up of bought time (ADR 0003): the row's Validity (never shortening) and
// Pause Limit, a redeem journal row, an audit row and a credit intent, in one
// transaction. It is never Sales and leaves the HotSpot profile alone. On an
// expired Time Credit it creates a replacement for the Device, carries the
// Points there, then deducts. The row is matched by cost and time, so a table
// edited after the Portal rendered it refuses rather than sells another row.
func (l *Ledger) RedeemPoints(id, mac, ip string, table []PointsTier, pointsMilli, seconds int64) (TimeCredit, error) {
	var row *PointsTier
	for i := range table {
		if table[i].PointsMilli == pointsMilli && table[i].Seconds == seconds {
			row = &table[i]
			break
		}
	}
	if row == nil {
		return TimeCredit{}, ErrPointsRowUnknown
	}
	tx, err := l.db.Begin()
	if err != nil {
		return TimeCredit{}, err
	}
	defer tx.Rollback()
	tc, err := scanTimeCredit(tx.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = ?", id))
	if err != nil {
		return tc, err
	}
	if tc.PointsMilli < row.PointsMilli {
		return tc, ErrPointsTooLow
	}
	if err := ExtendValidity(tx, tc.ID, row.ValiditySeconds); errors.Is(err, ErrTimeCreditExpired) {
		// Forfeited time stays forfeited; the Points move to a new Time Credit.
		expired := tc.ID
		if tc, err = insertReservedTimeCredit(tx, mac, ip); err != nil {
			return tc, err
		}
		if err := carryPoints(tx, expired, tc.ID); err != nil {
			return tc, err
		}
		if err := ExtendValidity(tx, tc.ID, row.ValiditySeconds); err != nil {
			return tc, err
		}
	} else if err != nil {
		return tc, err
	}
	before := tc.BoughtSeconds
	if before > 315360000-row.Seconds {
		return tc, errors.New("Time Credit purchase limit reached")
	}
	tc.BoughtSeconds += row.Seconds
	if _, err := tx.Exec("UPDATE time_credits SET bought_seconds = ? WHERE id = ?", tc.BoughtSeconds, tc.ID); err != nil {
		return tc, err
	}
	if err := resetPauses(tx, tc.ID, row.PauseLimit); err != nil {
		return tc, err
	}
	ref := fmt.Sprintf("points-row:%d:%d", row.PointsMilli, row.Seconds)
	if err := addPoints(tx, tc.ID, -row.PointsMilli, "redeem", ref); err != nil {
		return tc, err
	}
	// audit.source is unique, so it names this redemption's journal row, not
	// the Points table row, which can be redeemed again.
	var journalID int64
	if err := tx.QueryRow("SELECT last_insert_rowid()").Scan(&journalID); err != nil {
		return tc, err
	}
	if err := tx.QueryRow("SELECT points_milli FROM time_credits WHERE id = ?", tc.ID).Scan(&tc.PointsMilli); err != nil {
		return tc, err
	}
	if _, err := tx.Exec(`INSERT INTO audit (at, actor, reason, time_credit_id, before_seconds, after_seconds, source)
	 VALUES (?, ?, 'points-redemption', ?, ?, ?, ?)`, time.Now().UTC().Format(time.RFC3339Nano), "device:"+mac, tc.ID, before, tc.BoughtSeconds, fmt.Sprintf("points-journal:%d", journalID)); err != nil {
		return tc, err
	}
	if err := QueueCreditIntent(tx, tc.ID, tc.BoughtSeconds); err != nil {
		return tc, err
	}
	return tc, tx.Commit()
}

// addPoints changes a balance and journals it, inside the Top-up's transaction.
func addPoints(tx *sql.Tx, id string, delta int64, reason, ref string) error {
	if _, err := tx.Exec("UPDATE time_credits SET points_milli = points_milli + ? WHERE id = ?", delta, id); err != nil {
		return err
	}
	_, err := tx.Exec("INSERT INTO points_journal (time_credit_id, delta_milli, reason, ref, at) VALUES (?, ?, ?, ?, ?)",
		id, delta, reason, ref, time.Now().UTC().Format(time.RFC3339Nano))
	return err
}

// carryPoints moves an expired Time Credit's whole balance to the Time Credit
// replacing it, with a journal row on each side. An empty balance is a no-op,
// so carrying twice is harmless.
func carryPoints(tx *sql.Tx, from, to string) error {
	var balance int64
	if err := tx.QueryRow("SELECT points_milli FROM time_credits WHERE id = ?", from).Scan(&balance); err != nil || balance == 0 {
		return err
	}
	if err := addPoints(tx, from, -balance, "carry-out", to); err != nil {
		return err
	}
	return addPoints(tx, to, balance, "carry-in", from)
}

func (l *Ledger) pointsJournal() ([]PointsEntry, error) {
	rows, err := l.db.Query("SELECT id, time_credit_id, delta_milli, reason, ref, at FROM points_journal ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	entries := []PointsEntry{}
	for rows.Next() {
		var e PointsEntry
		if err := rows.Scan(&e.ID, &e.TimeCreditID, &e.DeltaMilli, &e.Reason, &e.Ref, &e.At); err != nil {
			return nil, err
		}
		entries = append(entries, e)
	}
	return entries, rows.Err()
}
