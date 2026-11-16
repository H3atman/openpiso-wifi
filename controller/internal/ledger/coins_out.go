package ledger

import (
	"errors"
	"time"
)

// ErrNothingToCollect refuses a Coins Out that would include no new coins,
// such as a second post of the same confirmation.
var ErrNothingToCollect = errors.New("no coins since the last Coins Out")

// ErrCoinsOutAhead refuses a Coins Out through coins the Vendo never reported.
var ErrCoinsOutAhead = errors.New("Coins Out is past the Vendo's reported coins")

// VendoSales is one Vendo's coin Sales in pesos (1 pulse = ₱1), counted over
// the intake journal, so coins dropped outside a Coin Window are included.
type VendoSales struct {
	Total   int64 // every coin reported
	Current int64 // coins after the last Coins Out
	// Through is the latest intake journal row; Coins Out records up to it.
	Through int64
	// LastCoinsOut is the latest Coins Out, if any (Pesos 0 means none).
	LastCoinsOut CoinsOut
}

// CoinsOut is one append-only collection record.
type CoinsOut struct {
	Pesos      int64
	RecordedAt string // RFC 3339
}

func (l *Ledger) initializeCoinsOut() error {
	_, err := l.db.Exec(`CREATE TABLE IF NOT EXISTS vendo_coins_out (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		vendo_id INTEGER NOT NULL REFERENCES vendos(id),
		through_delta_id INTEGER NOT NULL, pesos INTEGER NOT NULL CHECK (pesos > 0),
		recorded_at TEXT NOT NULL
	)`)
	return err
}

// VendoSales reads every Vendo's Sales by Vendo ID.
func (l *Ledger) VendoSales() (map[uint32]VendoSales, error) {
	rows, err := l.db.Query(`SELECT v.id,
	 COALESCE((SELECT SUM(coins) FROM vendo_coin_deltas d WHERE d.vendo_id = v.id), 0),
	 COALESCE((SELECT SUM(coins) FROM vendo_coin_deltas d WHERE d.vendo_id = v.id AND d.id > COALESCE(o.through_delta_id, 0)), 0),
	 COALESCE((SELECT MAX(id) FROM vendo_coin_deltas d WHERE d.vendo_id = v.id), 0),
	 COALESCE(o.pesos, 0), COALESCE(o.recorded_at, '')
	 FROM vendos v LEFT JOIN vendo_coins_out o ON o.id = (SELECT MAX(id) FROM vendo_coins_out WHERE vendo_id = v.id)`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	sales := map[uint32]VendoSales{}
	for rows.Next() {
		var id uint32
		var sale VendoSales
		if err := rows.Scan(&id, &sale.Total, &sale.Current, &sale.Through, &sale.LastCoinsOut.Pesos, &sale.LastCoinsOut.RecordedAt); err != nil {
			return nil, err
		}
		sales[id] = sale
	}
	return sales, rows.Err()
}

// RecordCoinsOut closes a Vendo's collection period at intake journal row
// through: the row the Operator's confirmation was computed from, so the
// recorded amount is the one confirmed even if coins arrived since.
func (l *Ledger) RecordCoinsOut(vendoID uint32, through int64) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var latest, previous, pesos int64
	if err := tx.QueryRow(`SELECT
	 COALESCE((SELECT MAX(id) FROM vendo_coin_deltas WHERE vendo_id = ?), 0),
	 COALESCE((SELECT through_delta_id FROM vendo_coins_out WHERE vendo_id = ? ORDER BY id DESC LIMIT 1), 0)`,
		vendoID, vendoID).Scan(&latest, &previous); err != nil {
		return err
	}
	if through > latest {
		return ErrCoinsOutAhead
	}
	if err := tx.QueryRow("SELECT COALESCE(SUM(coins), 0) FROM vendo_coin_deltas WHERE vendo_id = ? AND id > ? AND id <= ?",
		vendoID, previous, through).Scan(&pesos); err != nil {
		return err
	}
	if pesos == 0 {
		return ErrNothingToCollect
	}
	if _, err := tx.Exec("INSERT INTO vendo_coins_out (vendo_id, through_delta_id, pesos, recorded_at) VALUES (?, ?, ?, ?)",
		vendoID, through, pesos, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		return err
	}
	return tx.Commit()
}
