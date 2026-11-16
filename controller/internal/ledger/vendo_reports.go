package ledger

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/vendoprotocol"
)

var ErrVendoDisabled = errors.New("Vendo is not paired")

func (l *Ledger) initializeVendoReports() error {
	for _, statement := range []string{
		`CREATE TABLE IF NOT EXISTS vendo_totals (
			vendo_id INTEGER NOT NULL REFERENCES vendos(id), epoch INTEGER NOT NULL,
			last_seen INTEGER NOT NULL, PRIMARY KEY (vendo_id, epoch)
		)`,
		`CREATE TABLE IF NOT EXISTS vendo_coin_deltas (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			vendo_id INTEGER NOT NULL REFERENCES vendos(id), epoch INTEGER NOT NULL,
			total INTEGER NOT NULL, window_id INTEGER NOT NULL, coins INTEGER NOT NULL CHECK (coins > 0),
			tag INTEGER NOT NULL, recorded_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS vendo_audit (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			vendo_id INTEGER NOT NULL, reason TEXT NOT NULL, epoch INTEGER NOT NULL,
			total INTEGER NOT NULL, window_id INTEGER NOT NULL, coins INTEGER NOT NULL,
			recorded_at TEXT NOT NULL
		)`,
	} {
		if _, err := l.db.Exec(statement); err != nil {
			return err
		}
	}
	return l.initializeCoinsOut()
}

// RecordVendoReport commits each positive serial delta and its high water mark
// together with Coin Window allocation, Top-up audit and credit intent. Unissued
// tags never credit a Time Credit. No Router operation belongs in this path.
func (l *Ledger) RecordVendoReport(ctx context.Context, report vendoprotocol.Message) error {
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var status string
	var nonce, rseq uint32
	if err := tx.QueryRowContext(ctx, "SELECT status, boot_nonce, rseq FROM vendos WHERE id = ?", report.VendoID).Scan(&status, &nonce, &rseq); err != nil {
		return err
	}
	if status != "paired" {
		return ErrVendoDisabled
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	audit := func(reason string, coins uint32) error {
		_, err := tx.ExecContext(ctx, `INSERT INTO vendo_audit
			(vendo_id, reason, epoch, total, window_id, coins, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)`,
			report.VendoID, reason, report.Epoch, report.Total, report.WindowID, coins, now)
		return err
	}
	// A new boot_nonce means the Vendo booted since its last report: Up since restarts.
	booted := report.BootNonce != nonce
	if booted || int32(report.RSeq-rseq) > 0 {
		nonce, rseq = report.BootNonce, report.RSeq
	} else if report.RSeq != rseq {
		if err := audit("stale_freshness", 0); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE vendos SET last_contact = ?, boot_nonce = ?, rseq = ?,
		booted_at = CASE WHEN ? THEN ? ELSE booted_at END WHERE id = ?`,
		now, nonce, rseq, booted, now, report.VendoID); err != nil {
		return err
	}
	var last uint32
	err = tx.QueryRowContext(ctx, "SELECT last_seen FROM vendo_totals WHERE vendo_id = ? AND epoch = ?", report.VendoID, report.Epoch).Scan(&last)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if _, err := tx.ExecContext(ctx, "INSERT OR IGNORE INTO vendo_totals (vendo_id, epoch, last_seen) VALUES (?, ?, 0)", report.VendoID, report.Epoch); err != nil {
		return err
	}
	delta := int32(report.Total - last)
	if delta > 0 {
		if _, err := tx.ExecContext(ctx, `INSERT INTO vendo_coin_deltas
			(vendo_id, epoch, total, window_id, coins, tag, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)`,
			report.VendoID, report.Epoch, report.Total, report.WindowID, uint32(delta), report.Tag, now); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "UPDATE vendo_totals SET last_seen = ? WHERE vendo_id = ? AND epoch = ?", report.Total, report.VendoID, report.Epoch); err != nil {
			return err
		}
	} else if delta < 0 {
		if err := audit("stale_total", 0); err != nil {
			return err
		}
	}
	allocated, err := allocateCoinReport(tx, report, max(delta, 0))
	if err != nil {
		return err
	}
	if !allocated && delta > 0 {
		if err := audit("unassigned_window", uint32(delta)); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// Rejection audit stores only bounded, classified metadata, never raw payloads,
// MACs, secrets or attacker-controlled strings. Unknown IDs intentionally have no FK.
func (l *Ledger) RejectVendo(id uint32, reason string) error {
	_, err := l.db.Exec(`INSERT INTO vendo_audit (vendo_id, reason, epoch, total, window_id, coins, recorded_at)
		VALUES (?, ?, 0, 0, 0, 0, ?)`, id, reason, time.Now().UTC().Format(time.RFC3339Nano))
	return err
}

type VendoTotal struct {
	VendoID  uint32 `json:"vendo_id"`
	Epoch    uint32 `json:"epoch"`
	LastSeen uint32 `json:"last_seen"`
}

type VendoCoinDelta struct {
	ID         int64  `json:"id"`
	VendoID    uint32 `json:"vendo_id"`
	Epoch      uint32 `json:"epoch"`
	Total      uint32 `json:"total"`
	WindowID   uint32 `json:"window_id"`
	Coins      uint32 `json:"coins"`
	Tag        byte   `json:"tag"`
	RecordedAt string `json:"recorded_at"`
}

type VendoAudit struct {
	ID         int64  `json:"id"`
	VendoID    uint32 `json:"vendo_id"`
	Reason     string `json:"reason"`
	Epoch      uint32 `json:"epoch"`
	Total      uint32 `json:"total"`
	WindowID   uint32 `json:"window_id"`
	Coins      uint32 `json:"coins"`
	RecordedAt string `json:"recorded_at"`
}

type VendoProtocolState struct {
	Totals     []VendoTotal     `json:"totals"`
	CoinDeltas []VendoCoinDelta `json:"coin_deltas"`
	Audit      []VendoAudit     `json:"audit"`
}

func (l *Ledger) VendoProtocolState() (VendoProtocolState, error) {
	state := VendoProtocolState{Totals: []VendoTotal{}, CoinDeltas: []VendoCoinDelta{}, Audit: []VendoAudit{}}
	rows, err := l.db.Query("SELECT vendo_id, epoch, last_seen FROM vendo_totals ORDER BY vendo_id, epoch")
	if err != nil {
		return state, err
	}
	for rows.Next() {
		var row VendoTotal
		if err := rows.Scan(&row.VendoID, &row.Epoch, &row.LastSeen); err != nil {
			rows.Close()
			return state, err
		}
		state.Totals = append(state.Totals, row)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return state, err
	}
	rows.Close()
	// Keep public snapshots bounded without deleting any durable evidence.
	rows, err = l.db.Query(`SELECT id, vendo_id, epoch, total, window_id, coins, tag, recorded_at
		FROM (SELECT * FROM vendo_coin_deltas ORDER BY id DESC LIMIT 1000) ORDER BY id`)
	if err != nil {
		return state, err
	}
	for rows.Next() {
		var row VendoCoinDelta
		if err := rows.Scan(&row.ID, &row.VendoID, &row.Epoch, &row.Total, &row.WindowID, &row.Coins, &row.Tag, &row.RecordedAt); err != nil {
			rows.Close()
			return state, err
		}
		state.CoinDeltas = append(state.CoinDeltas, row)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return state, err
	}
	rows.Close()
	rows, err = l.db.Query(`SELECT id, vendo_id, reason, epoch, total, window_id, coins, recorded_at
		FROM (SELECT * FROM vendo_audit ORDER BY id DESC LIMIT 1000) ORDER BY id`)
	if err != nil {
		return state, err
	}
	defer rows.Close()
	for rows.Next() {
		var row VendoAudit
		if err := rows.Scan(&row.ID, &row.VendoID, &row.Reason, &row.Epoch, &row.Total, &row.WindowID, &row.Coins, &row.RecordedAt); err != nil {
			return state, err
		}
		state.Audit = append(state.Audit, row)
	}
	return state, rows.Err()
}
