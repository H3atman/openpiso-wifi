package ledger

import (
	"database/sql"
	"errors"
	"strings"
	"time"
)

const oversightSchema = `
CREATE TABLE IF NOT EXISTS audit_resolutions (
 audit_id INTEGER PRIMARY KEY REFERENCES vendo_audit(id),
 status TEXT NOT NULL CHECK(status IN ('dismissed', 'granted')),
 resolved_at TEXT NOT NULL, resolved_by TEXT NOT NULL, reason TEXT NOT NULL,
 grant_audit_id INTEGER UNIQUE REFERENCES audit(id),
 CHECK((status = 'dismissed' AND grant_audit_id IS NULL) OR (status = 'granted' AND grant_audit_id IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS audit_resolutions_no_update BEFORE UPDATE ON audit_resolutions BEGIN SELECT RAISE(ABORT, 'append-only resolution'); END;
CREATE TRIGGER IF NOT EXISTS audit_resolutions_no_delete BEFORE DELETE ON audit_resolutions BEGIN SELECT RAISE(ABORT, 'append-only resolution'); END;
CREATE TRIGGER IF NOT EXISTS vendo_audit_no_update BEFORE UPDATE ON vendo_audit BEGIN SELECT RAISE(ABORT, 'append-only Vendo audit'); END;
CREATE TRIGGER IF NOT EXISTS vendo_audit_no_delete BEFORE DELETE ON vendo_audit BEGIN SELECT RAISE(ABORT, 'append-only Vendo audit'); END;
`

type AuditReview struct {
	VendoAudit
	Status           string `json:"status"`
	ResolvedAt       string `json:"resolved_at"`
	ResolvedBy       string `json:"resolved_by"`
	ResolutionReason string `json:"resolution_reason"`
	GrantAuditID     int64  `json:"grant_audit_id"`
}

// Review is a separate append-only resolution, never a rewrite of the evidence.
// No age, restart or automatic reconciliation can mark a row resolved.
func (l *Ledger) AuditReview() ([]AuditReview, error) {
	rows, err := l.db.Query(`SELECT a.id, a.vendo_id, a.reason, a.epoch, a.total, a.window_id, a.coins, a.recorded_at,
		COALESCE(r.status, 'pending-review'), COALESCE(r.resolved_at, ''), COALESCE(r.resolved_by, ''),
		COALESCE(r.reason, ''), COALESCE(r.grant_audit_id, 0)
		FROM vendo_audit a LEFT JOIN audit_resolutions r ON r.audit_id = a.id ORDER BY a.id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	review := []AuditReview{}
	for rows.Next() {
		var row AuditReview
		if err := rows.Scan(&row.ID, &row.VendoID, &row.Reason, &row.Epoch, &row.Total, &row.WindowID, &row.Coins, &row.RecordedAt,
			&row.Status, &row.ResolvedAt, &row.ResolvedBy, &row.ResolutionReason, &row.GrantAuditID); err != nil {
			return nil, err
		}
		review = append(review, row)
	}
	return review, rows.Err()
}

func pendingReview(tx *sql.Tx, auditID int64) error {
	var status sql.NullString
	if err := tx.QueryRow(`SELECT r.status FROM vendo_audit a LEFT JOIN audit_resolutions r ON r.audit_id = a.id
		WHERE a.id = ?`, auditID).Scan(&status); err != nil {
		return err
	}
	if status.Valid {
		return ErrAlreadyResolved
	}
	return nil
}

func (l *Ledger) DismissAudit(auditID int64, reason string) error {
	if auditID < 1 || strings.TrimSpace(reason) == "" || len(reason) > 500 {
		return ErrReviewInvalid
	}
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := pendingReview(tx, auditID); err != nil {
		return err
	}
	if _, err := tx.Exec(`INSERT INTO audit_resolutions (audit_id, status, resolved_at, resolved_by, reason)
		VALUES (?, 'dismissed', ?, 'admin', ?)`, auditID, time.Now().UTC().Format(time.RFC3339), strings.TrimSpace(reason)); err != nil {
		return err
	}
	return tx.Commit()
}

type SalesReport struct {
	VoucherPesos     int64 `json:"voucher_pesos"`
	RedeemedVouchers int64 `json:"redeemed_vouchers"`
	UnpricedVouchers int64 `json:"unpriced_vouchers"`
	CoinPesos        int64 `json:"coin_pesos"`
}

// Total is the Sales in pesos: redeemed Voucher face value plus coins received.
func (r SalesReport) Total() int64 { return r.VoucherPesos + r.CoinPesos }

// Sales counts redeemed Voucher face value and durable positive coin deltas,
// never audit copies or time grants. Missing historical prices remain unknown.
// julianday compares RFC3339 offsets correctly; the period is [from, to).
func (l *Ledger) Sales(from, to time.Time) (SalesReport, error) {
	tx, err := l.db.Begin()
	if err != nil {
		return SalesReport{}, err
	}
	defer tx.Rollback()
	report, err := salesTotals(tx, from, to, 0, "")
	if err != nil {
		return report, err
	}
	return report, tx.Commit()
}

func salesTotals(tx *sql.Tx, from, to time.Time, vendoID uint32, seller string) (SalesReport, error) {
	var report SalesReport
	start, end := from.UTC().Format(time.RFC3339Nano), to.UTC().Format(time.RFC3339Nano)
	if vendoID == 0 {
		query := `SELECT COALESCE(SUM(p.pesos), 0), COUNT(*),
			COALESCE(SUM(CASE WHEN p.pesos IS NULL THEN 1 ELSE 0 END), 0)
			FROM vouchers v JOIN voucher_batches b ON b.id = v.batch_id LEFT JOIN voucher_prices p ON p.batch_id = v.batch_id
			WHERE v.redeemed_at IS NOT NULL AND julianday(v.redeemed_at) >= julianday(?) AND julianday(v.redeemed_at) < julianday(?)`
		args := []any{start, end}
		if seller != "" {
			query += " AND COALESCE(NULLIF(b.seller, ''), 'Shop') = ?"
			args = append(args, seller)
		}
		err := tx.QueryRow(query, args...).Scan(&report.VoucherPesos, &report.RedeemedVouchers, &report.UnpricedVouchers)
		if err != nil {
			return report, err
		}
	}
	if seller != "" {
		return report, nil
	}
	query := `SELECT COALESCE(SUM(coins), 0) FROM vendo_coin_deltas
		WHERE julianday(recorded_at) >= julianday(?) AND julianday(recorded_at) < julianday(?)`
	args := []any{start, end}
	if vendoID != 0 {
		query += " AND vendo_id = ?"
		args = append(args, vendoID)
	}
	err := tx.QueryRow(query, args...).Scan(&report.CoinPesos)
	return report, err
}

var ErrGrantInvalid = errors.New("choose 1–31536000 seconds, a reason and a request ID")
var ErrAlreadyResolved = errors.New("request already recorded or audit already resolved")
var ErrReviewInvalid = errors.New("choose an audit row and give a resolution reason")

// GrantTime is a Top-up, never a deduction. Its audit and absolute Router target
// commit with the bought-time change, so a failed Router write is retryable.
func (l *Ledger) GrantTime(id string, seconds int64, reason, requestID string, auditID int64) (TimeCredit, error) {
	if auditID < 0 {
		return TimeCredit{}, ErrReviewInvalid
	}
	if seconds < 1 || seconds > 31536000 || strings.TrimSpace(reason) == "" || len(reason) > 500 || requestID == "" || len(requestID) > 100 {
		return TimeCredit{}, ErrGrantInvalid
	}
	tx, err := l.db.Begin()
	if err != nil {
		return TimeCredit{}, err
	}
	defer tx.Rollback()
	if auditID != 0 {
		if err := pendingReview(tx, auditID); err != nil {
			return TimeCredit{}, err
		}
	}
	var count int
	source := "admin-grant:" + requestID
	if err := tx.QueryRow("SELECT COUNT(*) FROM audit WHERE source = ?", source).Scan(&count); err != nil {
		return TimeCredit{}, err
	}
	if count != 0 {
		return TimeCredit{}, ErrAlreadyResolved
	}
	tc, err := scanTimeCredit(tx.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = ?", id))
	if err != nil {
		return tc, err
	}
	before := tc.BoughtSeconds
	if before > 315360000-seconds {
		return tc, ErrGrantInvalid
	}
	validity, err := siteValiditySeconds(tx)
	if err != nil {
		return tc, err
	}
	if err := ExtendValidity(tx, id, validity); err != nil {
		return tc, err
	}
	tc.BoughtSeconds += seconds
	if _, err := tx.Exec("UPDATE time_credits SET bought_seconds = ? WHERE id = ?", tc.BoughtSeconds, id); err != nil {
		return tc, err
	}
	pauses, err := pauseSettings(tx) // Vouchers and admin grants store the site Pause Limit
	if err != nil {
		return tc, err
	}
	if err := resetPauses(tx, id, pauses.PauseLimit); err != nil {
		return tc, err
	}
	now := time.Now().UTC().Format(time.RFC3339)
	result, err := tx.Exec(`INSERT INTO audit (at, actor, reason, time_credit_id, before_seconds, after_seconds, source)
		VALUES (?, 'admin', ?, ?, ?, ?, ?)`, now, "admin-grant: "+strings.TrimSpace(reason), id, before, tc.BoughtSeconds, source)
	if err != nil {
		return tc, err
	}
	if auditID != 0 {
		grantID, err := result.LastInsertId()
		if err != nil {
			return tc, err
		}
		if _, err := tx.Exec(`INSERT INTO audit_resolutions (audit_id, status, resolved_at, resolved_by, reason, grant_audit_id)
			VALUES (?, 'granted', ?, 'admin', ?, ?)`, auditID, now, strings.TrimSpace(reason), grantID); err != nil {
			return tc, err
		}
	}
	if err := QueueCreditIntent(tx, id, tc.BoughtSeconds); err != nil {
		return tc, err
	}
	return tc, tx.Commit()
}
