package ledger

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/vendoprotocol"
)

// creditCoinWindow prices the entire saved total. Its difference from the last
// price is a Top-up, so a late pulse can cross a tier without under-crediting.
func creditCoinWindow(tx *sql.Tx, id uint32) error {
	var timeCreditID string
	var ratesJSON []byte
	var coins, credited, earned int64
	if err := tx.QueryRow("SELECT time_credit_id, rates, coins, credited_seconds, points_milli FROM coin_windows WHERE id = ?", id).Scan(
		&timeCreditID, &ratesJSON, &coins, &credited, &earned); err != nil {
		return err
	}
	var tiers []RateTier
	if err := json.Unmarshal(ratesJSON, &tiers); err != nil {
		return err
	}
	// The tier that priced the window sets this Top-up's Validity and Pause Limit.
	tier, priced := pricingTier(tiers, coins)
	seconds := priced - credited
	if seconds <= 0 {
		return nil
	}
	// Retired time is forfeited: coins after expiry buy a new Time Credit for
	// the window's Device, containing only this credit, never a revived one.
	// Its Points move to the replacement (ADR 0006).
	if err := ExtendValidity(tx, timeCreditID, tier.ValiditySeconds); errors.Is(err, ErrTimeCreditExpired) {
		var mac, ip string
		if err := tx.QueryRow("SELECT mac, ip FROM coin_windows WHERE id = ?", id).Scan(&mac, &ip); err != nil {
			return err
		}
		replacement, err := insertReservedTimeCredit(tx, mac, ip)
		if err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE coin_windows SET time_credit_id = ? WHERE id = ?", replacement.ID, id); err != nil {
			return err
		}
		if err := carryPoints(tx, timeCreditID, replacement.ID); err != nil {
			return err
		}
		timeCreditID = replacement.ID
		if err := ExtendValidity(tx, timeCreditID, tier.ValiditySeconds); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	var before int64
	if err := tx.QueryRow("SELECT bought_seconds FROM time_credits WHERE id = ?", timeCreditID).Scan(&before); err != nil {
		return err
	}
	after := before + seconds
	if after < before {
		return errors.New("Time Credit bought time overflow")
	}
	if _, err := tx.Exec("UPDATE time_credits SET bought_seconds = ? WHERE id = ?", after, timeCreditID); err != nil {
		return err
	}
	if err := resetPauses(tx, timeCreditID, tier.PauseLimit); err != nil {
		return err
	}
	if err := setCoinTopUpProfile(tx, timeCreditID, id); err != nil {
		return err
	}
	// Like seconds, Points price the whole window: a late coin adds only the
	// difference from what the window already earned, rounded down, never less.
	points := coins * tier.PointsMilli / tier.Pesos
	if points > earned {
		if err := addPoints(tx, timeCreditID, points-earned, "coin-earn", fmt.Sprintf("coin-window:%d:%d", id, coins)); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE coin_windows SET points_milli = ? WHERE id = ?", points, id); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`INSERT INTO audit (at, actor, reason, time_credit_id, before_seconds, after_seconds, source)
	 VALUES (?, 'vendo', 'coin-top-up', ?, ?, ?, ?)`, time.Now().UTC().Format(time.RFC3339Nano), timeCreditID, before, after, fmt.Sprintf("coin-window:%d:%d", id, priced)); err != nil {
		return err
	}
	if err := QueueCreditIntent(tx, timeCreditID, after); err != nil {
		return err
	}
	_, err := tx.Exec(`UPDATE coin_windows SET credited_seconds = ?,
	 valid_until = (SELECT valid_until FROM time_credit_validity WHERE time_credit_id = ?)
	 WHERE id = ?`, priced, timeCreditID, id)
	return err
}

// allocateCoinReport is part of RecordVendoReport's high-water transaction.
// Closed must be processed even if its cumulative total adds no new coins.
func allocateCoinReport(tx *sql.Tx, report vendoprotocol.Message, delta int32) (bool, error) {
	var status string
	err := tx.QueryRow("SELECT status FROM coin_windows WHERE id = ? AND vendo_id = ?", report.WindowID, report.VendoID).Scan(&status)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	now := time.Now().UnixMilli()
	if delta > 0 {
		if _, err := tx.Exec("UPDATE coin_windows SET coins = coins + ?, last_coin_at = ? WHERE id = ?", delta, now, report.WindowID); err != nil {
			return true, err
		}
	}
	if report.Tag == vendoprotocol.Closed {
		if _, err := tx.Exec(`UPDATE coin_windows SET status = 'settled',
		 close_at = CASE WHEN close_at = 0 THEN ? ELSE close_at END,
		 closed_at = CASE WHEN closed_at = 0 THEN ? ELSE closed_at END WHERE id = ?`, now, now, report.WindowID); err != nil {
			return true, err
		}
		status = "settled"
	}
	if status == "settled" {
		return true, creditCoinWindow(tx, report.WindowID)
	}
	return true, nil
}

func (l *Ledger) CloseCoinWindow(id uint32, timeCreditID string) error {
	result, err := l.db.Exec(`UPDATE coin_windows SET status = 'closing', close_at = ?
	 WHERE id = ? AND time_credit_id = ? AND status = 'open'`, time.Now().UnixMilli(), id, timeCreditID)
	if err != nil {
		return err
	}
	if n, err := result.RowsAffected(); err != nil || n != 0 {
		return err
	}
	var owner string
	if err := l.db.QueryRow("SELECT time_credit_id FROM coin_windows WHERE id = ?", id).Scan(&owner); err != nil {
		return err
	}
	if owner != timeCreditID {
		return errors.New("Coin Window belongs to another Time Credit")
	}
	return nil
}

// ReconcileCoinWindows runs before Router access: even an outage must not hold
// accepted coins hostage. Grace settlement does not release the Vendo until its
// Closed arrives; commands keep retrying Close while that receipt is outstanding.
func (l *Ledger) ReconcileCoinWindows(boot bool) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := time.Now().UnixMilli()
	if _, err := tx.Exec(`UPDATE coin_windows SET status = 'closing', close_at = ?
	 WHERE status = 'open' AND (? OR last_coin_at <= ?)`, now, boot, now-CoinWindowIdle.Milliseconds()); err != nil {
		return err
	}
	rows, err := tx.Query("SELECT id FROM coin_windows WHERE status = 'closing' AND close_at <= ?", now-CoinWindowGrace.Milliseconds())
	if err != nil {
		return err
	}
	ids := []uint32{}
	for rows.Next() {
		var id uint32
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
		if err := creditCoinWindow(tx, id); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE coin_windows SET status = 'settled' WHERE id = ?", id); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// CoinWindowCommand uses the newest authenticated freshness pair, not the
// report being acknowledged (which may itself be a resend).
func (l *Ledger) CoinWindowCommand(vendoID uint32) (*vendoprotocol.Message, error) {
	var id uint32
	var status string
	var lastCoinAt int64
	err := l.db.QueryRow("SELECT id, status, last_coin_at FROM coin_windows WHERE vendo_id = ? AND closed_at = 0", vendoID).Scan(&id, &status, &lastCoinAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	// Only a new coin renews the safety cap. Heartbeats/resends carry its
	// remaining duration; busy customers are not forced out after ten minutes.
	remaining := lastCoinAt + CoinWindowCapMS - time.Now().UnixMilli()
	if status == "open" && remaining <= 0 {
		var owner string
		if err := l.db.QueryRow("SELECT time_credit_id FROM coin_windows WHERE id = ?", id).Scan(&owner); err != nil {
			return nil, err
		}
		if err := l.CloseCoinWindow(id, owner); err != nil {
			return nil, err
		}
		status = "closing"
	}
	vendo, err := l.Vendo(vendoID)
	if err != nil {
		return nil, err
	}
	command := &vendoprotocol.Message{Tag: vendoprotocol.Close, VendoID: vendoID, WindowID: id, BootNonce: vendo.BootNonce, RSeq: vendo.RSeq}
	if status == "open" {
		command.Tag = vendoprotocol.Open
		command.ExpiresMS = uint32(remaining)
	}
	return command, nil
}
