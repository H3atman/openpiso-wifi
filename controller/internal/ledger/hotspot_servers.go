package ledger

import (
	"fmt"
)

// adoptAllSetting marks a ledger from before Router setup: it has a saved Router
// connection, and the next boot Adopts every HotSpot server on that Router (ADR 0011).
const adoptAllSetting = "adopt_all_hotspot_servers"

// initializeAdoptedServers creates the set of Adopted HotSpot servers, keyed by
// server name. Creating it in a ledger that already has a Router connection
// records that the Controller managed every server before this table existed.
func (l *Ledger) initializeAdoptedServers() error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var exists bool
	if err := tx.QueryRow("SELECT count(*) > 0 FROM sqlite_master WHERE type = 'table' AND name = 'adopted_hotspot_servers'").Scan(&exists); err != nil {
		return err
	}
	if exists {
		return nil
	}
	if _, err := tx.Exec("CREATE TABLE adopted_hotspot_servers (name TEXT PRIMARY KEY)"); err != nil {
		return err
	}
	if _, err := tx.Exec("INSERT INTO settings (key, value) SELECT ?, '1' FROM router_config", adoptAllSetting); err != nil {
		return err
	}
	return tx.Commit()
}

// AdoptedHotspotServers lists the HotSpot servers the Controller manages, by name.
func (l *Ledger) AdoptedHotspotServers() ([]string, error) {
	rows, err := l.db.Query("SELECT name FROM adopted_hotspot_servers ORDER BY name")
	if err != nil {
		return nil, fmt.Errorf("read Adopted HotSpot servers: %w", err)
	}
	defer rows.Close()
	names := []string{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		names = append(names, name)
	}
	return names, rows.Err()
}

// AdoptAllPending reports whether this ledger predates Router setup and its
// Router's HotSpot servers have not yet been recorded as Adopted.
func (l *Ledger) AdoptAllPending() (bool, error) {
	var pending bool
	if err := l.db.QueryRow("SELECT count(*) > 0 FROM settings WHERE key = ?", adoptAllSetting).Scan(&pending); err != nil {
		return false, fmt.Errorf("read Router setup migration: %w", err)
	}
	return pending, nil
}

// AdoptHotspotServers adds names to the Adopted HotSpot servers and ends a
// pending AdoptAllPending.
func (l *Ledger) AdoptHotspotServers(names []string) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, name := range names {
		if _, err := tx.Exec("INSERT OR IGNORE INTO adopted_hotspot_servers (name) VALUES (?)", name); err != nil {
			return fmt.Errorf("Adopt HotSpot server %s: %w", name, err)
		}
	}
	if _, err := tx.Exec("DELETE FROM settings WHERE key = ?", adoptAllSetting); err != nil {
		return err
	}
	return tx.Commit()
}
