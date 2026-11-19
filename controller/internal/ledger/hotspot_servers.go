package ledger

import (
	"database/sql"
	"errors"
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

// ForgetHotspotServers empties the Adopted HotSpot servers and their Portal
// arrivals, and ends a pending AdoptAllPending. Fresh start calls it before it
// deletes every server, so the ledger never names a server the Router lacks.
func (l *Ledger) ForgetHotspotServers() error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, statement := range []string{"DELETE FROM adopted_hotspot_servers", "DELETE FROM portal_arrivals", "DELETE FROM settings WHERE key = '" + adoptAllSetting + "'"} {
		if _, err := tx.Exec(statement); err != nil {
			return fmt.Errorf("forget HotSpot servers: %w", err)
		}
	}
	return tx.Commit()
}

// ForgetHotspotServer removes name from the Adopted HotSpot servers, with its
// Portal arrivals. Router setup calls it only for a server missing on the Router.
func (l *Ledger) ForgetHotspotServer(name string) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, statement := range []string{"DELETE FROM adopted_hotspot_servers WHERE name = ?", "DELETE FROM portal_arrivals WHERE server = ?"} {
		if _, err := tx.Exec(statement, name); err != nil {
			return fmt.Errorf("forget HotSpot server %s: %w", name, err)
		}
	}
	return tx.Commit()
}

// FreshStartBlocker says why Fresh start must not run, or "" when it may: the
// wipe deletes the Router users that hold Time Credits' time and the IP
// bindings that let paired Vendos through.
func (l *Ledger) FreshStartBlocker() (string, error) {
	var credits, vendos int
	if err := l.db.QueryRow("SELECT (SELECT count(*) FROM time_credits), (SELECT count(*) FROM vendos WHERE status != 'unpaired')").Scan(&credits, &vendos); err != nil {
		return "", fmt.Errorf("read Time Credits and Vendos: %w", err)
	}
	switch {
	case credits > 0:
		return fmt.Sprintf("The ledger has %d Time Credit(s); Fresh start would delete the Router users that hold their time.", credits), nil
	case vendos > 0:
		return fmt.Sprintf("%d Vendo(s) are paired; Fresh start would delete the IP bindings that let them through. Unpair them first.", vendos), nil
	}
	return "", nil
}

// routerBackupSetting is the name of the Router backup file saved at the last connect.
const routerBackupSetting = "router_backup"

// SaveRouterBackup records the backup file connecting saved on the Router.
func (l *Ledger) SaveRouterBackup(name string) error {
	if _, err := l.db.Exec("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", routerBackupSetting, name); err != nil {
		return fmt.Errorf("record Router backup: %w", err)
	}
	return nil
}

// RouterBackup is the backup file saved at the last connect, "" when none was recorded.
func (l *Ledger) RouterBackup() (string, error) {
	var name string
	err := l.db.QueryRow("SELECT value FROM settings WHERE key = ?", routerBackupSetting).Scan(&name)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("read Router backup: %w", err)
	}
	return name, nil
}

// portalArrivalSchema records each HotSpot server a Device has been sent to the
// Portal from, the Router setup checklist's last item for that server.
const portalArrivalSchema = `CREATE TABLE IF NOT EXISTS portal_arrivals (server TEXT PRIMARY KEY)`

// RecordPortalArrival notes that a Device reached the Portal from HotSpot server server.
func (l *Ledger) RecordPortalArrival(server string) error {
	if _, err := l.db.Exec("INSERT OR IGNORE INTO portal_arrivals (server) VALUES (?)", server); err != nil {
		return fmt.Errorf("record Portal arrival from %s: %w", server, err)
	}
	return nil
}

// PortalArrived reports whether a Device has reached the Portal from HotSpot server server.
func (l *Ledger) PortalArrived(server string) (bool, error) {
	var arrived bool
	if err := l.db.QueryRow("SELECT count(*) > 0 FROM portal_arrivals WHERE server = ?", server).Scan(&arrived); err != nil {
		return false, fmt.Errorf("read Portal arrivals: %w", err)
	}
	return arrived, nil
}
