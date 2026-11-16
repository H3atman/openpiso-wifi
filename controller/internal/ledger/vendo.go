package ledger

import (
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
)

var ErrVendoConflict = errors.New("Vendo MAC or IP is already paired")
var ErrVendoNotFound = errors.New("Vendo not found")

// ErrVendoDeactivated refuses a new Coin Window on a Deactivated Vendo.
var ErrVendoDeactivated = errors.New("Vendo is deactivated")

type Vendo struct {
	Name          string `json:"name"`
	Location      string `json:"location"`
	HotspotServer string `json:"hotspot_server"`
	// HotspotProfile is the Router profile this Vendo's coin Top-ups give a
	// Time Credit (GLOSSARY "HotSpot profile"); empty means the base profiles.
	HotspotProfile string `json:"hotspot_profile"`
	ID             uint32 `json:"id"`
	MAC            string `json:"mac"`
	IP             string `json:"ip"`
	Status         string `json:"status"`
	// Active is false while the Operator has Deactivated a paired Vendo: it
	// stays paired and reporting, but opens no new Coin Windows.
	Active      bool   `json:"active"`
	LastContact string `json:"last_contact"`
	// BootedAt is when the Controller first saw the current boot_nonce (RFC
	// 3339): the Vendo's "Up since", accurate to within one heartbeat.
	BootedAt  string `json:"booted_at"`
	BootNonce uint32 `json:"boot_nonce"`
	RSeq      uint32 `json:"rseq"`
	Secret    []byte `json:"-"`
	// RateTiers are the Vendo's own Rate tiers; nil means it uses the site's.
	RateTiers []RateTier `json:"rate_tiers"`
	// PointsTiers is the Vendo's own Points table; nil means it uses the site's.
	PointsTiers []PointsTier `json:"points_tiers"`
}

// decodeTiers reads the Vendo's own Rate tiers and Points table; a NULL
// column leaves the field nil, so the Vendo uses the site's. A customized
// Points table may be empty, and stays non-nil.
func (v *Vendo) decodeTiers(rates, points []byte) error {
	if rates != nil {
		if err := json.Unmarshal(rates, &v.RateTiers); err != nil {
			return err
		}
	}
	if points != nil {
		v.PointsTiers = []PointsTier{}
		return json.Unmarshal(points, &v.PointsTiers)
	}
	return nil
}

// DisplayName keeps unnamed and historical Vendos identifiable in reports.
func (v Vendo) DisplayName() string {
	name := v.Name
	if name == "" {
		name = "Vendo " + strconv.FormatUint(uint64(v.ID), 10)
	}
	if v.Status == "unpaired" {
		name += " (unpaired)"
	}
	return name
}

func (l *Ledger) initializeVendos() error {
	for _, statement := range []string{
		`CREATE TABLE IF NOT EXISTS vendos (
			id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id BETWEEN 1 AND 4294967295),
			mac TEXT NOT NULL, ip TEXT NOT NULL, secret BLOB NOT NULL CHECK (length(secret) = 32),
			status TEXT NOT NULL CHECK (status IN ('provisioned', 'pending', 'paired', 'unpairing', 'unpaired')),
			last_contact TEXT NOT NULL DEFAULT '', boot_nonce INTEGER NOT NULL DEFAULT 0,
			rseq INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE UNIQUE INDEX IF NOT EXISTS vendo_mac ON vendos(mac) WHERE status != 'unpaired'`,
	} {
		if _, err := l.db.Exec(statement); err != nil {
			return err
		}
	}
	var columns int
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'hotspot_server'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN hotspot_server TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}

	// SQLite cannot extend a CHECK constraint in place. Rebuild without renaming
	// the old table, so existing Coin Window/report foreign keys keep their target.
	var schema string
	if err := l.db.QueryRow("SELECT sql FROM sqlite_master WHERE name = 'vendos'").Scan(&schema); err != nil {
		return err
	}
	if !strings.Contains(schema, "'provisioned'") {
		if err := l.migrateProvisionedVendos(); err != nil {
			return err
		}
	}
	for _, statement := range []string{
		"CREATE UNIQUE INDEX IF NOT EXISTS vendo_ip ON vendos(ip) WHERE status != 'unpaired' AND ip != ''",
		"CREATE UNIQUE INDEX IF NOT EXISTS vendo_mac ON vendos(mac) WHERE status != 'unpaired'",
	} {
		if _, err := l.db.Exec(statement); err != nil {
			return err
		}
	}
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'name'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN name TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'location'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN location TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	// A column, not a status: Deactivate keeps the Vendo paired, and the status
	// CHECK constraint could only grow through a table rebuild.
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'active'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))"); err != nil {
			return err
		}
	}
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'booted_at'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN booted_at TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'hotspot_profile'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN hotspot_profile TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	// NULL means the Vendo uses the site Rate tiers (coin_rate_tiers).
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'rate_tiers'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN rate_tiers BLOB"); err != nil {
			return err
		}
	}
	// NULL means the Vendo uses the site Points table (points_tiers).
	if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('vendos') WHERE name = 'points_tiers'").Scan(&columns); err != nil {
		return err
	}
	if columns == 0 {
		if _, err := l.db.Exec("ALTER TABLE vendos ADD COLUMN points_tiers BLOB"); err != nil {
			return err
		}
	}
	return l.initializeVendoReports()
}

// PrepareVendo persists flash credentials without changing the Router.
func (l *Ledger) PrepareVendo(mac string) (Vendo, error) {
	tx, err := l.db.Begin()
	if err != nil {
		return Vendo{}, err
	}
	defer tx.Rollback()
	var existing uint32
	err = tx.QueryRow("SELECT id FROM vendos WHERE status != 'unpaired' AND mac = ?", mac).Scan(&existing)
	if err == nil {
		return Vendo{}, ErrVendoConflict
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return Vendo{}, err
	}
	vendo := Vendo{MAC: mac, Status: "provisioned", Active: true, Secret: make([]byte, 32)}
	if _, err := rand.Read(vendo.Secret); err != nil {
		return Vendo{}, err
	}
	result, err := tx.Exec("INSERT INTO vendos (mac, ip, secret, status) VALUES (?, '', ?, 'provisioned')", mac, vendo.Secret)
	if err != nil {
		return Vendo{}, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return Vendo{}, err
	}
	vendo.ID = uint32(id)
	return vendo, tx.Commit()
}

// StageVendoPairing pins verified network identity before any retryable Router
// changes. Pending Vendos may own partial Router rules and need normal cleanup.
func (l *Ledger) StageVendoPairing(id uint32, ip, server string) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var count int
	if err := tx.QueryRow("SELECT count(*) FROM vendos WHERE id != ? AND ip = ? AND status != 'unpaired'", id, ip).Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrVendoConflict
	}
	if _, err := tx.Exec("UPDATE vendos SET ip = ?, hotspot_server = ?, status = 'pending' WHERE id = ? AND status = 'provisioned'", ip, server, id); err != nil {
		return err
	}
	return tx.Commit()
}

func (l *Ledger) migrateProvisionedVendos() error {
	// Open uses one connection and runs migrations before starting the server.
	if _, err := l.db.Exec("PRAGMA foreign_keys=OFF"); err != nil {
		return err
	}
	defer l.db.Exec("PRAGMA foreign_keys=ON")
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, statement := range []string{
		`CREATE TABLE vendos_new (
			id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id BETWEEN 1 AND 4294967295),
			mac TEXT NOT NULL, ip TEXT NOT NULL, secret BLOB NOT NULL CHECK (length(secret) = 32),
			status TEXT NOT NULL CHECK (status IN ('provisioned', 'pending', 'paired', 'unpairing', 'unpaired')),
			last_contact TEXT NOT NULL DEFAULT '', boot_nonce INTEGER NOT NULL DEFAULT 0,
			rseq INTEGER NOT NULL DEFAULT 0, hotspot_server TEXT NOT NULL DEFAULT ''
		)`,
		"INSERT INTO vendos_new SELECT id, mac, ip, secret, status, last_contact, boot_nonce, rseq, hotspot_server FROM vendos",
		"DROP TABLE vendos",
		"ALTER TABLE vendos_new RENAME TO vendos",
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (l *Ledger) Vendo(id uint32) (Vendo, error) {
	var vendo Vendo
	var rates, points []byte
	err := l.db.QueryRow(`SELECT id, mac, ip, secret, status, active, last_contact, booted_at, boot_nonce, rseq, hotspot_server, name, location, hotspot_profile, rate_tiers, points_tiers
		FROM vendos WHERE id = ?`, id).Scan(&vendo.ID, &vendo.MAC, &vendo.IP, &vendo.Secret, &vendo.Status, &vendo.Active,
		&vendo.LastContact, &vendo.BootedAt, &vendo.BootNonce, &vendo.RSeq, &vendo.HotspotServer, &vendo.Name, &vendo.Location, &vendo.HotspotProfile, &rates, &points)
	if errors.Is(err, sql.ErrNoRows) {
		return Vendo{}, ErrVendoNotFound
	}
	if err != nil {
		return vendo, err
	}
	return vendo, vendo.decodeTiers(rates, points)
}

func (l *Ledger) SetVendoStatus(id uint32, status string) error {
	_, err := l.db.Exec("UPDATE vendos SET status = ? WHERE id = ?", status, id)
	return err
}

func (l *Ledger) Vendos() ([]Vendo, error) {
	rows, err := l.db.Query(`SELECT id, mac, ip, status, active, last_contact, booted_at, boot_nonce, rseq, hotspot_server, name, location, hotspot_profile, rate_tiers, points_tiers FROM vendos ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	vendos := []Vendo{}
	for rows.Next() {
		var vendo Vendo
		var rates, points []byte
		if err := rows.Scan(&vendo.ID, &vendo.MAC, &vendo.IP, &vendo.Status, &vendo.Active, &vendo.LastContact, &vendo.BootedAt, &vendo.BootNonce, &vendo.RSeq, &vendo.HotspotServer, &vendo.Name, &vendo.Location, &vendo.HotspotProfile, &rates, &points); err != nil {
			return nil, err
		}
		if err := vendo.decodeTiers(rates, points); err != nil {
			return nil, err
		}
		vendos = append(vendos, vendo)
	}
	return vendos, rows.Err()
}

// EditVendo saves the Edit dialog's Details: Name, Location, HotSpot server
// and HotSpot profile in one statement, so a save never lands half-applied.
func (l *Ledger) EditVendo(id uint32, name, location, server, profile string) error {
	_, err := l.db.Exec("UPDATE vendos SET name = ?, location = ?, hotspot_server = ?, hotspot_profile = ? WHERE id = ?", name, location, server, profile, id)
	return err
}

// SetVendoActive Deactivates (false) or Reactivates (true) a Vendo. Only the
// flag changes: secret, Router rules, Name and Sales stay, and a Coin Window
// already open settles normally.
func (l *Ledger) SetVendoActive(id uint32, active bool) error {
	_, err := l.db.Exec("UPDATE vendos SET active = ? WHERE id = ?", active, id)
	return err
}
