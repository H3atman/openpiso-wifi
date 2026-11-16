package ledger

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"time"
)

const salesSchema = `
CREATE INDEX IF NOT EXISTS sales_coin_date ON vendo_coin_deltas(julianday(recorded_at), id);
CREATE INDEX IF NOT EXISTS sales_coin_vendo_date ON vendo_coin_deltas(vendo_id, julianday(recorded_at), id);
CREATE INDEX IF NOT EXISTS sales_window_closed ON coin_windows(closed_at, id) WHERE status = 'settled';
CREATE INDEX IF NOT EXISTS sales_window_vendo_closed ON coin_windows(vendo_id, closed_at, id) WHERE status = 'settled';
CREATE INDEX IF NOT EXISTS sales_voucher_date ON vouchers(julianday(redeemed_at), code) WHERE redeemed_at IS NOT NULL;
`

// Sale snapshots are nullable: purchases before this migration stay unknown,
// rather than borrowing a Device or Validity that may have changed since then.
func (l *Ledger) initializeSales() error {
	for _, column := range []struct {
		table, name, kind string
	}{
		{"coin_windows", "valid_until", "INTEGER"},
		{"vouchers", "redeemed_mac", "TEXT"},
		{"vouchers", "redeemed_ip", "TEXT"},
		{"vouchers", "valid_until", "INTEGER"},
	} {
		var columns int
		if err := l.db.QueryRow("SELECT count(*) FROM pragma_table_info(?) WHERE name = ?", column.table, column.name).Scan(&columns); err != nil {
			return err
		}
		if columns == 0 {
			if _, err := l.db.Exec("ALTER TABLE " + column.table + " ADD COLUMN " + column.name + " " + column.kind); err != nil {
				return err
			}
		}
	}
	_, err := l.db.Exec(salesSchema)
	return err
}

// SalesFilter uses a half-open period. A zero VendoID includes all Vendos and
// Vouchers; a nonzero ID includes only that Vendo's coins. Search narrows rows
// and their count, never the receipt-based totals.
type SalesFilter struct {
	From, To time.Time
	VendoID  uint32
	Seller   string
	Search   string
	Page     int
	PageSize int
}

type Sale struct {
	ID              string    `json:"id"`
	Seller          string    `json:"seller"`
	At              time.Time `json:"at"`
	IP              string    `json:"ip"`
	MAC             string    `json:"mac"`
	User            string    `json:"user"`
	Validity        string    `json:"validity"`
	Type            string    `json:"type"`
	VendoID         uint32    `json:"vendo_id"`
	VendoName       string    `json:"vendo_name"`
	CreditedSeconds int64     `json:"credited_seconds"`
	Amount          *int64    `json:"amount"`
}

type SalesPage struct {
	SalesReport
	Rows     []Sale `json:"rows"`
	Count    int64  `json:"count"`
	Page     int    `json:"page"`
	PageSize int    `json:"page_size"`
}

// Both count and rows use this relation; receipts intentionally contribute only
// to Total Sales, not to the settled-Window purchase table. Build UTC Voucher
// milliseconds from whole seconds and truncated fractions, avoiding date-function
// rounding and Julian-day floating-point drift at midnight.
// The original UTC time (without Z so whole seconds sort before fractions)
// orders redemptions within a millisecond; identity breaks exact-time ties.
const salesRows = `WITH candidates AS (
 SELECT closed_at AS at, 'coin' AS kind, CAST(id AS TEXT) AS id,
 ip, mac, time_credit_id AS user, vendo_id, credited_seconds, coins AS amount, valid_until, NULL AS redeemed_at, '' AS seller
 FROM coin_windows WHERE status = 'settled' AND closed_at >= ? AND closed_at < ?%s
 UNION ALL
 SELECT unixepoch(substr(v.redeemed_at, 1, 19) || 'Z') * 1000
 + CAST(substr(rtrim(v.redeemed_at, 'Z') || '000', 21, 3) AS INTEGER),
 'voucher', v.code, COALESCE(v.redeemed_ip, ''), COALESCE(v.redeemed_mac, ''), v.code, 0, b.seconds, p.pesos, v.valid_until, rtrim(v.redeemed_at, 'Z'), COALESCE(NULLIF(b.seller, ''), 'Shop')
 FROM vouchers v JOIN voucher_batches b ON b.id = v.batch_id
 LEFT JOIN voucher_prices p ON p.batch_id = v.batch_id
 WHERE v.redeemed_at IS NOT NULL AND julianday(v.redeemed_at) >= julianday(?) AND julianday(v.redeemed_at) < julianday(?) AND ? = 0
), filtered AS (SELECT * FROM candidates%s) `

// QuerySales reads one capped page, the matching count and receipt-based totals
// in one short transaction. It releases the ledger before rendering or writing HTTP.
func (l *Ledger) QuerySales(filter SalesFilter) (SalesPage, error) {
	page := SalesPage{Rows: []Sale{}, Page: filter.Page, PageSize: filter.PageSize}
	if page.Page < 1 {
		page.Page = 1
	}
	if page.PageSize != 25 && page.PageSize != 50 && page.PageSize != 100 {
		page.PageSize = 25
	}
	tx, err := l.db.Begin()
	if err != nil {
		return page, err
	}
	defer tx.Rollback()
	page.SalesReport, err = salesTotals(tx, filter.From, filter.To, filter.VendoID, filter.Seller)
	if err != nil {
		return page, err
	}
	relation, args := salesRelation(filter)
	if err := tx.QueryRow(relation+`SELECT COUNT(*) FROM filtered`, args...).Scan(&page.Count); err != nil {
		return page, err
	}
	last := max(int((page.Count+int64(page.PageSize)-1)/int64(page.PageSize)), 1)
	page.Page = min(page.Page, last)
	args = append(args, page.PageSize, (int64(page.Page)-1)*int64(page.PageSize))
	rows, err := tx.Query(relation+salesSelect+` LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		row, err := scanSale(rows)
		if err != nil {
			return page, err
		}
		page.Rows = append(page.Rows, row)
	}
	if err := rows.Err(); err != nil {
		return page, err
	}
	if err := rows.Close(); err != nil {
		return page, err
	}
	return page, tx.Commit()
}

func salesRelation(filter SalesFilter) (string, []any) {
	args := []any{filter.From.UnixMilli(), filter.To.UnixMilli()}
	coinVendo := ""
	if filter.VendoID != 0 {
		coinVendo = " AND vendo_id = ?"
		args = append(args, filter.VendoID)
	}
	args = append(args, filter.From.UTC().Format(time.RFC3339Nano), filter.To.UTC().Format(time.RFC3339Nano), filter.VendoID)
	where := " WHERE 1 = 1"
	if filter.Seller != "" {
		where += " AND kind = 'voucher' AND seller = ?"
		args = append(args, filter.Seller)
	}
	if filter.Search != "" {
		// instr matches literal substrings, including SQL wildcard characters.
		where += ` AND (instr(lower(ip), lower(?)) > 0 OR instr(lower(user), lower(?)) > 0
 OR instr(lower(replace(mac, '-', ':')), lower(replace(?, '-', ':'))) > 0)`
		args = append(args, filter.Search, filter.Search, filter.Search)
	}
	return fmt.Sprintf(salesRows, coinVendo, where), args
}

const salesSelect = `SELECT f.id, f.at, f.ip, f.mac, f.user, f.kind, f.vendo_id, f.credited_seconds, f.amount, f.valid_until,
 COALESCE(v.name, ''), COALESCE(v.status, ''), f.seller
 FROM filtered f LEFT JOIN vendos v ON v.id = f.vendo_id
 ORDER BY f.at DESC, f.kind DESC, f.redeemed_at DESC, f.id DESC`

func scanSale(rows *sql.Rows) (Sale, error) {
	var row Sale
	var at int64
	var validUntil sql.NullInt64
	var vendo Vendo
	if err := rows.Scan(&row.ID, &at, &row.IP, &row.MAC, &row.User, &row.Type, &row.VendoID, &row.CreditedSeconds, &row.Amount, &validUntil, &vendo.Name, &vendo.Status, &row.Seller); err != nil {
		return row, err
	}
	if row.VendoID != 0 {
		vendo.ID = row.VendoID
		row.VendoName = vendo.DisplayName()
	}
	if validUntil.Valid {
		row.Validity = time.Unix(validUntil.Int64, 0).In(time.Local).Format(time.RFC3339)
	}
	row.At = time.UnixMilli(at).In(time.Local)
	return row, nil
}

// ExportSales visits the full filtered relation on a dedicated read-only
// connection. Its single snapshot is time-limited so it cannot pin the WAL
// indefinitely. The caller buffers output, never writing to the network here.
func (l *Ledger) ExportSales(ctx context.Context, filter SalesFilter, visit func(Sale) error) error {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	source := url.URL{Scheme: "file", Path: l.path, RawQuery: "mode=ro"}
	db, err := sql.Open("sqlite", source.String())
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	relation, args := salesRelation(filter)
	rows, err := db.QueryContext(ctx, relation+salesSelect, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		row, err := scanSale(rows)
		if err != nil {
			return err
		}
		if err := visit(row); err != nil {
			return err
		}
	}
	return rows.Err()
}
