package ledger

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const CoinWindowCapMS = 600000
const CoinWindowIdle = 60 * time.Second
const CoinWindowGrace = 5 * time.Second

var ErrVendoBusy = errors.New("Vendo busy")

// RateTier prices coins and sets the Validity, Pause Limit and Points of the
// coin Top-up it prices (ADR 0006). "No limit" Validity is MaxValiditySeconds.
// PointsMilli is earned per Pesos, in thousandths; omitted when 0, so tiers
// without Points keep their pre-Points shape.
type RateTier struct {
	Pesos           int64 `json:"pesos"`
	Seconds         int64 `json:"seconds"`
	ValiditySeconds int64 `json:"validity_seconds"`
	PauseLimit      int   `json:"pause_limit"`
	PointsMilli     int64 `json:"points_milli,omitempty"`
}

// pricingTier is the highest tier the coins reach. It prices the whole Coin
// Window, rounded down to seconds; increasing seconds per peso makes late
// credit monotonic.
func pricingTier(tiers []RateTier, coins int64) (RateTier, int64) {
	var tier RateTier
	var seconds int64
	for _, candidate := range tiers {
		if coins >= candidate.Pesos {
			tier, seconds = candidate, coins*candidate.Seconds/candidate.Pesos
		}
	}
	return tier, seconds
}

func (l *Ledger) RateTiers() ([]RateTier, error) {
	var data []byte
	if err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'coin_rate_tiers'").Scan(&data); err != nil {
		return nil, err
	}
	var tiers []RateTier
	err := json.Unmarshal(data, &tiers)
	return tiers, err
}

func (l *Ledger) SaveRateTiers(tiers []RateTier) error {
	data, err := rateTiersJSON(tiers)
	if err != nil {
		return err
	}
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec("UPDATE settings SET value = ? WHERE key = 'coin_rate_tiers'", data); err != nil {
		return err
	}
	// Marks the Rate tiers saved for Next steps, even at the shipped price.
	if _, err := tx.Exec("INSERT OR IGNORE INTO settings (key, value) VALUES ('rate_tiers_saved', '1')"); err != nil {
		return err
	}
	return tx.Commit()
}

// CustomizeVendoRates gives a Vendo its own copy of the site Rate tiers, to
// edit with SaveVendoRateTiers.
func (l *Ledger) CustomizeVendoRates(id uint32) error {
	_, err := l.db.Exec("UPDATE vendos SET rate_tiers = (SELECT value FROM settings WHERE key = 'coin_rate_tiers') WHERE id = ?", id)
	return err
}

// SaveVendoRateTiers sets a Vendo's own Rate tiers, under the site tiers' rules.
func (l *Ledger) SaveVendoRateTiers(id uint32, tiers []RateTier) error {
	data, err := rateTiersJSON(tiers)
	if err != nil {
		return err
	}
	_, err = l.db.Exec("UPDATE vendos SET rate_tiers = ? WHERE id = ?", data, id)
	return err
}

// UseSiteRates drops a Vendo's own Rate tiers; its next Coin Window takes the site's.
func (l *Ledger) UseSiteRates(id uint32) error {
	_, err := l.db.Exec("UPDATE vendos SET rate_tiers = NULL WHERE id = ?", id)
	return err
}

// rateTiersJSON validates Rate tiers, site or Vendo, and encodes them for storage.
func rateTiersJSON(tiers []RateTier) ([]byte, error) {
	if len(tiers) == 0 || len(tiers) > 100 || tiers[0].Pesos != 1 {
		return nil, errors.New("start with a ₱1 tier; choose up to 100 tiers")
	}
	for i, tier := range tiers {
		if tier.Pesos < 1 || tier.Pesos > 1000000 || tier.Seconds < 1 || tier.Seconds > 31536000 {
			return nil, errors.New("choose 1–1000000 pesos and 1–31536000 seconds")
		}
		if tier.ValiditySeconds < 1 || tier.ValiditySeconds > MaxValiditySeconds || tier.PauseLimit < 0 || tier.PauseLimit > 1000 {
			return nil, errors.New("choose a Validity of 1 second–36500 days (or No limit) and a Pause Limit of 0–1000")
		}
		if tier.PointsMilli < 0 || tier.PointsMilli > 1000000*1000 {
			return nil, errors.New("choose 0–1000000 Points")
		}
		if i > 0 {
			previous := tiers[i-1]
			if tier.Pesos <= previous.Pesos || tier.Seconds*previous.Pesos < previous.Seconds*tier.Pesos {
				return nil, errors.New("tiers must increase in pesos and never reduce seconds per peso")
			}
			// A re-priced window keeps the Points it already earned, so a
			// higher tier giving fewer Points per peso would over-award.
			if tier.PointsMilli*previous.Pesos < previous.PointsMilli*tier.Pesos {
				return nil, errors.New("a higher tier must never give fewer Points per peso than the tier before it")
			}
		}
	}
	return json.Marshal(tiers)
}

func (l *Ledger) initializeCoinWindows() error {
	// A reservation creates a Time Credit before any money arrives. Upgrade
	// the original positive-only constraint without changing identity columns.
	var schema string
	if err := l.db.QueryRow("SELECT sql FROM sqlite_master WHERE name = 'time_credits'").Scan(&schema); err != nil {
		return err
	}
	if strings.Contains(schema, "CHECK(bought_seconds > 0)") {
		if _, err := l.db.Exec("PRAGMA foreign_keys=OFF"); err != nil {
			return err
		}
		err := func() error {
			tx, err := l.db.Begin()
			if err != nil {
				return err
			}
			defer tx.Rollback()
			for _, statement := range []string{
				`CREATE TABLE time_credits_coin_upgrade (
				 id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, router_password TEXT NOT NULL,
				 bought_seconds INTEGER NOT NULL CHECK(bought_seconds >= 0), mac TEXT NOT NULL, ip TEXT NOT NULL)`,
				"INSERT INTO time_credits_coin_upgrade SELECT id, token, router_password, bought_seconds, mac, ip FROM time_credits",
				"DROP TABLE time_credits",
				"ALTER TABLE time_credits_coin_upgrade RENAME TO time_credits",
			} {
				if _, err := tx.Exec(statement); err != nil {
					return err
				}
			}
			return tx.Commit()
		}()
		_, enableErr := l.db.Exec("PRAGMA foreign_keys=ON")
		if err != nil {
			return err
		}
		if enableErr != nil {
			return enableErr
		}
	}
	_, err := l.db.Exec(`
	INSERT OR IGNORE INTO settings (key, value) VALUES ('coin_rate_tiers', '[{"pesos":1,"seconds":60}]');
	CREATE TABLE IF NOT EXISTS coin_windows (
	 id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id BETWEEN 1 AND 4294967295),
	 vendo_id INTEGER NOT NULL REFERENCES vendos(id), time_credit_id TEXT NOT NULL REFERENCES time_credits(id),
	 mac TEXT NOT NULL, ip TEXT NOT NULL, rates BLOB NOT NULL,
	 status TEXT NOT NULL CHECK(status IN ('open','closing','settled')),
	 coins INTEGER NOT NULL DEFAULT 0, credited_seconds INTEGER NOT NULL DEFAULT 0,
	 opened_at INTEGER NOT NULL, last_coin_at INTEGER NOT NULL,
	 close_at INTEGER NOT NULL DEFAULT 0, closed_at INTEGER NOT NULL DEFAULT 0
	);
	CREATE UNIQUE INDEX IF NOT EXISTS coin_window_vendo_live ON coin_windows(vendo_id) WHERE closed_at = 0;
	`)
	return err
}

type CoinWindow struct {
	ID              uint32 `json:"id"`
	VendoID         uint32 `json:"vendo_id"`
	TimeCreditID    string `json:"time_credit_id"`
	MAC             string `json:"mac"`
	IP              string `json:"ip"`
	Status          string `json:"status"`
	Coins           int64  `json:"coins"`
	CreditedSeconds int64  `json:"credited_seconds"`
	OpenedAt        int64  `json:"opened_at"`
	LastCoinAt      int64  `json:"last_coin_at"`
	CloseAt         int64  `json:"close_at"`
	ClosedAt        int64  `json:"closed_at"`
}

const coinWindowColumns = "id, vendo_id, time_credit_id, mac, ip, status, coins, credited_seconds, opened_at, last_coin_at, close_at, closed_at"

func (l *Ledger) CoinWindows() ([]CoinWindow, error) {
	rows, err := l.db.Query("SELECT " + coinWindowColumns + " FROM coin_windows ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	windows := []CoinWindow{}
	for rows.Next() {
		var w CoinWindow
		if err := rows.Scan(&w.ID, &w.VendoID, &w.TimeCreditID, &w.MAC, &w.IP, &w.Status, &w.Coins, &w.CreditedSeconds, &w.OpenedAt, &w.LastCoinAt, &w.CloseAt, &w.ClosedAt); err != nil {
			return nil, err
		}
		windows = append(windows, w)
	}
	return windows, rows.Err()
}

// insertReservedTimeCredit creates a zero-bought Time Credit with its Resume
// Code and Validity row, so portal views work before any coin arrives. It is
// never projected to the Router until a credit makes bought_seconds positive.
func insertReservedTimeCredit(tx *sql.Tx, mac, ip string) (TimeCredit, error) {
	tc := TimeCredit{MAC: mac, IP: ip}
	var err error
	for _, field := range []*string{&tc.ID, &tc.Token, &tc.RouterPassword} {
		*field, err = randomToken()
		if err != nil {
			return tc, err
		}
	}
	if _, err := tx.Exec("INSERT INTO time_credits (id, token, router_password, bought_seconds, mac, ip) VALUES (?, ?, ?, 0, ?, ?)", tc.ID, tc.Token, tc.RouterPassword, mac, ip); err != nil {
		return tc, err
	}
	if err := insertResumeCode(tx, tc.ID); err != nil {
		return tc, err
	}
	seconds, err := siteValiditySeconds(tx)
	if err != nil {
		return tc, err
	}
	return tc, ExtendValidity(tx, tc.ID, seconds)
}

// migrateRateTierLimits gives tiers saved before ADR 0006, in the settings and
// in Coin Window snapshots, the site Validity and Pause Limit, so behavior is
// unchanged until the Operator edits the rates. It also adds the per-Time
// Credit Pause Limit, where NULL (Time Credits never topped up since) means
// the site value.
func (l *Ledger) migrateRateTierLimits() error {
	var columns int
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('time_credit_pauses') WHERE name = 'pause_limit'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE time_credit_pauses ADD COLUMN pause_limit INTEGER"); err != nil {
			return err
		}
	}
	validity, err := l.ValiditySeconds()
	if err != nil {
		return err
	}
	pauses, err := l.PauseSettings()
	if err != nil {
		return err
	}
	fill := func(data []byte) ([]byte, bool, error) {
		var tiers []map[string]any
		if err := json.Unmarshal(data, &tiers); err != nil {
			return nil, false, err
		}
		changed := false
		for _, tier := range tiers {
			if _, ok := tier["validity_seconds"]; !ok {
				tier["validity_seconds"], tier["pause_limit"], changed = validity, pauses.PauseLimit, true
			}
		}
		data, err := json.Marshal(tiers)
		return data, changed, err
	}
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	type snapshot struct {
		id    int64
		rates []byte
	}
	snapshots := []snapshot{}
	rows, err := tx.Query("SELECT id, rates FROM coin_windows WHERE instr(CAST(rates AS TEXT), 'validity_seconds') = 0")
	if err != nil {
		return err
	}
	for rows.Next() {
		var s snapshot
		if err := rows.Scan(&s.id, &s.rates); err != nil {
			rows.Close()
			return err
		}
		snapshots = append(snapshots, s)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, s := range snapshots {
		rates, _, err := fill(s.rates)
		if err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE coin_windows SET rates = ? WHERE id = ?", rates, s.id); err != nil {
			return err
		}
	}
	var site []byte
	if err := tx.QueryRow("SELECT value FROM settings WHERE key = 'coin_rate_tiers'").Scan(&site); err != nil {
		return err
	}
	site, changed, err := fill(site)
	if err != nil {
		return err
	}
	if changed {
		if _, err := tx.Exec("UPDATE settings SET value = ? WHERE key = 'coin_rate_tiers'", site); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (l *Ledger) OpenCoinWindow(vendoID uint32, mac, ip, existingID string) (TimeCredit, error) {
	tx, err := l.db.Begin()
	if err != nil {
		return TimeCredit{}, err
	}
	defer tx.Rollback()
	var status string
	var active bool
	var rates []byte // the Vendo's own Rate tiers, else the site's
	if err := tx.QueryRow(`SELECT status, active, coalesce(rate_tiers, (SELECT value FROM settings WHERE key = 'coin_rate_tiers'))
	 FROM vendos WHERE id = ?`, vendoID).Scan(&status, &active, &rates); err != nil {
		return TimeCredit{}, err
	}
	if status != "paired" {
		return TimeCredit{}, ErrVendoDisabled
	}
	if !active {
		return TimeCredit{}, ErrVendoDeactivated
	}
	var busy int
	if err := tx.QueryRow("SELECT count(*) FROM coin_windows WHERE vendo_id = ? AND closed_at = 0", vendoID).Scan(&busy); err != nil {
		return TimeCredit{}, err
	}
	if busy != 0 {
		return TimeCredit{}, ErrVendoBusy
	}
	carryFrom := "" // the expired Time Credit whose Points move to the replacement
	if existingID != "" {
		var expiredAt, validUntil int64
		if err := tx.QueryRow("SELECT expired_at, valid_until FROM time_credit_validity WHERE time_credit_id = ?", existingID).Scan(&expiredAt, &validUntil); err != nil {
			return TimeCredit{}, err
		}
		if expiredAt != 0 || validUntil <= time.Now().Unix() {
			carryFrom, existingID = existingID, "" // never reserve a Vendo for retired time
		}
	}
	var tc TimeCredit
	if existingID != "" {
		tc, err = scanTimeCredit(tx.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = ?", existingID))
		if err != nil {
			return tc, err
		}
	} else {
		tc, err = insertReservedTimeCredit(tx, mac, ip)
		if err != nil {
			return tc, err
		}
		// Points move now, not with the first coin, so an abandoned window
		// still leaves them on the replacement (ADR 0006).
		if carryFrom != "" {
			if err := carryPoints(tx, carryFrom, tc.ID); err != nil {
				return tc, err
			}
			if err := tx.QueryRow("SELECT points_milli FROM time_credits WHERE id = ?", tc.ID).Scan(&tc.PointsMilli); err != nil {
				return tc, err
			}
		}
	}
	now := time.Now().UnixMilli()
	_, err = tx.Exec(`INSERT INTO coin_windows (vendo_id, time_credit_id, mac, ip, rates, status, opened_at, last_coin_at)
	 VALUES (?, ?, ?, ?, ?, 'open', ?, ?)`, vendoID, tc.ID, mac, ip, rates, now, now)
	if err != nil {
		return tc, fmt.Errorf("reserve Vendo: %w", err)
	}
	return tc, tx.Commit()
}
