package ledger

// CoinSales separates the intake journal from allocated time. Audit-only coins
// are included in the intake total, never counted again as additional sales.
type CoinSale struct {
	VendoID         uint32 `json:"vendo_id"`
	Coins           int64  `json:"coins"`
	UnassignedCoins int64  `json:"unassigned_coins"`
	CreditedSeconds int64  `json:"credited_seconds"`
}

func (l *Ledger) CoinSales() ([]CoinSale, error) {
	rows, err := l.db.Query(`SELECT v.id,
	 COALESCE((SELECT SUM(coins) FROM vendo_coin_deltas d WHERE d.vendo_id = v.id), 0),
	 COALESCE((SELECT SUM(coins) FROM vendo_audit a WHERE a.vendo_id = v.id AND reason = 'unassigned_window'), 0),
	 COALESCE((SELECT SUM(credited_seconds) FROM coin_windows w WHERE w.vendo_id = v.id), 0)
	 FROM vendos v ORDER BY v.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	sales := []CoinSale{}
	for rows.Next() {
		var sale CoinSale
		if err := rows.Scan(&sale.VendoID, &sale.Coins, &sale.UnassignedCoins, &sale.CreditedSeconds); err != nil {
			return nil, err
		}
		sales = append(sales, sale)
	}
	return sales, rows.Err()
}

type CoinGap struct {
	VendoID       uint32 `json:"vendo_id"`
	Epoch         uint32 `json:"epoch"`
	LastSeen      uint32 `json:"last_seen"`
	RecordedCoins int64  `json:"recorded_coins"`
}

// CoinGaps compares the complete journal, not bounded admin snapshots. Serial
// totals can wrap; their low 32 bits must still equal the sum of accepted deltas.
func (l *Ledger) CoinGaps() ([]CoinGap, error) {
	rows, err := l.db.Query(`SELECT t.vendo_id, t.epoch, t.last_seen, COALESCE(SUM(d.coins), 0)
	 FROM vendo_totals t LEFT JOIN vendo_coin_deltas d ON d.vendo_id = t.vendo_id AND d.epoch = t.epoch
	 GROUP BY t.vendo_id, t.epoch, t.last_seen`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	gaps := []CoinGap{}
	for rows.Next() {
		var gap CoinGap
		if err := rows.Scan(&gap.VendoID, &gap.Epoch, &gap.LastSeen, &gap.RecordedCoins); err != nil {
			return nil, err
		}
		if uint32(gap.RecordedCoins) != gap.LastSeen {
			gaps = append(gaps, gap)
		}
	}
	return gaps, rows.Err()
}
