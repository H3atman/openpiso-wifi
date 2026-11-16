package ledger

import (
	"errors"
	"fmt"
	"time"
)

// The Time Credit audit requires a credit; batch deletion has none. Keep its
// evidence separately, with the same append-only guarantees and Logs reader.
const voucherDeletionSchema = `
CREATE TABLE IF NOT EXISTS voucher_batch_deletions (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
 batch_id TEXT NOT NULL UNIQUE, batch_number INTEGER NOT NULL,
 count INTEGER NOT NULL, seller TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS voucher_batch_deletions_no_update BEFORE UPDATE ON voucher_batch_deletions BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
CREATE TRIGGER IF NOT EXISTS voucher_batch_deletions_no_delete BEFORE DELETE ON voucher_batch_deletions BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
`

var ErrVoucherBatchRedeemed = errors.New("cannot delete a batch with a redeemed Voucher")

// DeleteVoucherBatch checks redemption, removes the batch and records its
// evidence in one transaction. A refused or repeated delete writes no audit row.
func (l *Ledger) DeleteVoucherBatch(id string) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var number int64
	var seller string
	if err := tx.QueryRow("SELECT number, COALESCE(seller, '') FROM voucher_batches WHERE id = ?", id).Scan(&number, &seller); err != nil {
		return err
	}
	var count, redeemed int
	if err := tx.QueryRow(`SELECT COUNT(*), COUNT(CASE WHEN redeemed_at IS NOT NULL OR time_credit_id IS NOT NULL THEN 1 END) FROM vouchers WHERE batch_id = ?`, id).Scan(&count, &redeemed); err != nil {
		return err
	}
	if redeemed != 0 {
		return ErrVoucherBatchRedeemed
	}
	for _, statement := range []string{
		"DELETE FROM vouchers WHERE batch_id = ?",
		"DELETE FROM voucher_prices WHERE batch_id = ?",
		"DELETE FROM voucher_batches WHERE id = ?",
	} {
		if _, err := tx.Exec(statement, id); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`INSERT INTO voucher_batch_deletions (at, actor, batch_id, batch_number, count, seller) VALUES (?, 'admin', ?, ?, ?, ?)`, time.Now().UTC().Format(time.RFC3339Nano), id, number, count, seller); err != nil {
		return err
	}
	return tx.Commit()
}

func (l *Ledger) voucherDeletionActivity(q activityQuery) ([]ActivityEvent, error) {
	rows, err := l.db.Query(`SELECT id, at, actor, batch_number, count, seller FROM voucher_batch_deletions WHERE `+textSeconds("at")+` >= ? `+q.order(textSeconds("at"), "id")+` LIMIT ?`, q.since, q.limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []ActivityEvent
	for rows.Next() {
		var id, number, count int64
		var at, actor, seller string
		if err := rows.Scan(&id, &at, &actor, &number, &count, &seller); err != nil {
			return nil, err
		}
		if seller == "" {
			seller = "Shop"
		}
		events = append(events, ActivityEvent{
			Key: fmt.Sprintf("voucher-deletion-%d", id), At: parseActivityTime(at), Kind: ActivityAudit,
			Title: "Voucher Batch deleted", Detail: fmt.Sprintf("Batch #%d: %d %s · Seller: %s · %s", number, count, plural(count, "Voucher"), seller, actor),
		})
	}
	return events, rows.Err()
}
