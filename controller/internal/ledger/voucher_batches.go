package ledger

// VoucherBatches lists every batch newest first, with its Voucher counts.
func (l *Ledger) VoucherBatches() ([]VoucherBatch, error) {
	rows, err := l.db.Query(`SELECT b.id, b.seconds, b.created_at, p.pesos, b.number,
 COALESCE(b.tier_label, ''), b.validity_seconds, b.pause_limit,
 b.code_preset, b.code_length, b.code_prefix, COALESCE(b.seller, ''), COUNT(v.code), COUNT(v.redeemed_at)
 FROM voucher_batches b LEFT JOIN voucher_prices p ON p.batch_id = b.id
 LEFT JOIN vouchers v ON v.batch_id = b.id GROUP BY b.id ORDER BY b.number DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	batches := []VoucherBatch{}
	for rows.Next() {
		var b VoucherBatch
		if err := rows.Scan(&b.ID, &b.Seconds, &b.CreatedAt, &b.Pesos, &b.Number, &b.TierLabel, &b.ValiditySeconds, &b.PauseLimit, &b.CodePreset, &b.CodeLength, &b.CodePrefix, &b.Seller, &b.Count, &b.RedeemedCount); err != nil {
			return nil, err
		}
		batches = append(batches, b)
	}
	return batches, rows.Err()
}

// Add columns without rebuilding existing batches or changing their prices.
func (l *Ledger) migrateVoucherBatches() error {
	for _, column := range []struct{ name, definition string }{
		{"seller", "TEXT"}, {"number", "INTEGER"}, {"tier_label", "TEXT"},
		{"validity_seconds", "INTEGER"}, {"pause_limit", "INTEGER"},
		{"code_preset", "TEXT NOT NULL DEFAULT 'unambiguous'"},
		{"code_length", "INTEGER NOT NULL DEFAULT 8"}, {"code_prefix", "TEXT NOT NULL DEFAULT ''"},
	} {
		var count int
		if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info('voucher_batches') WHERE name = ?", column.name).Scan(&count); err != nil {
			return err
		}
		if count == 0 {
			if _, err := l.db.Exec("ALTER TABLE voucher_batches ADD COLUMN " + column.name + " " + column.definition); err != nil {
				return err
			}
		}
	}
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	validity, err := siteValiditySeconds(tx)
	if err != nil {
		return err
	}
	pauses, err := pauseSettings(tx)
	if err != nil {
		return err
	}
	_, err = tx.Exec(`WITH numbered AS (
 SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, rowid) AS n FROM voucher_batches
 ) UPDATE voucher_batches SET number = (SELECT n FROM numbered WHERE numbered.id = voucher_batches.id)
 WHERE number IS NULL`)
	if err != nil {
		return err
	}
	if _, err := tx.Exec(`UPDATE voucher_batches SET validity_seconds = COALESCE(validity_seconds, ?), pause_limit = COALESCE(pause_limit, ?)`, validity, pauses.PauseLimit); err != nil {
		return err
	}
	if _, err := tx.Exec("CREATE UNIQUE INDEX IF NOT EXISTS voucher_batch_number ON voucher_batches(number)"); err != nil {
		return err
	}
	if _, err := tx.Exec("CREATE UNIQUE INDEX IF NOT EXISTS voucher_code_nocase ON vouchers(code COLLATE NOCASE)"); err != nil {
		return err
	}
	return tx.Commit()
}

// VoucherSellers supplies previously used labels to Generate and Sales Inventory.
func (l *Ledger) VoucherSellers() ([]string, error) {
	rows, err := l.db.Query("SELECT DISTINCT seller FROM voucher_batches WHERE seller IS NOT NULL AND seller != '' ORDER BY seller COLLATE NOCASE")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	sellers := []string{}
	for rows.Next() {
		var seller string
		if err := rows.Scan(&seller); err != nil {
			return nil, err
		}
		sellers = append(sellers, seller)
	}
	return sellers, rows.Err()
}
