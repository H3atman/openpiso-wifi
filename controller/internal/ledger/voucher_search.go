package ledger

// VoucherMatch includes the batch terms and the Device recorded at redemption,
// which may differ from the Time Credit's Device after a Rebind.
type VoucherMatch struct {
	Batch       VoucherBatch
	Code        string
	RedeemedAt  string
	RedeemedMAC string
	RedeemedIP  string
}

// SearchVouchers matches literal code substrings across all batches, newest first.
func (l *Ledger) SearchVouchers(query string) ([]VoucherMatch, bool, error) {
	rows, err := l.db.Query(`SELECT v.code, COALESCE(v.redeemed_at, ''),
 COALESCE(v.redeemed_mac, ''), COALESCE(v.redeemed_ip, ''),
 b.id, b.number, COALESCE(b.seller, ''), b.seconds, p.pesos
 FROM vouchers v JOIN voucher_batches b ON b.id = v.batch_id
 LEFT JOIN voucher_prices p ON p.batch_id = b.id
 WHERE instr(lower(v.code), lower(?)) > 0
 ORDER BY b.number DESC, v.code LIMIT 51`, query)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	matches := []VoucherMatch{}
	for rows.Next() {
		var match VoucherMatch
		if err := rows.Scan(&match.Code, &match.RedeemedAt, &match.RedeemedMAC, &match.RedeemedIP,
			&match.Batch.ID, &match.Batch.Number, &match.Batch.Seller, &match.Batch.Seconds, &match.Batch.Pesos); err != nil {
			return nil, false, err
		}
		matches = append(matches, match)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	if len(matches) > 50 {
		return matches[:50], true, nil
	}
	return matches, false, nil
}
