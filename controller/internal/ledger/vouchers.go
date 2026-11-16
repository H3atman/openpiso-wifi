package ledger

import (
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"
)

const VoucherAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

const voucherLetters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
const voucherDigits = "0123456789"
const VoucherDefaultPreset = "unambiguous"

// VoucherCodeAlphabet supplies the minting alphabet and admin sample characters.
func VoucherCodeAlphabet(preset string) string {
	switch preset {
	case "digits":
		return voucherDigits
	case "uppercase":
		return voucherLetters
	case VoucherDefaultPreset:
		return VoucherAlphabet
	case "mixed":
		return voucherLetters + strings.ToLower(voucherLetters) + voucherDigits
	default:
		return ""
	}
}

// VoucherCodeSpaceSize counts characters after case-insensitive redemption.
func VoucherCodeSpaceSize(preset string) int64 {
	if preset == "mixed" {
		return 36
	}
	return int64(len(VoucherCodeAlphabet(preset)))
}

// ValidateCodeFormat applies the original format by default and enforces the
// 1% limit on case-insensitive code space. A fixed prefix adds no entropy.
func (b *VoucherBatch) ValidateCodeFormat(count int) error {
	if count < 1 || count > 1000 {
		return errors.New("choose 1–1000 Vouchers")
	}
	if b.CodePreset == "" && b.CodeLength == 0 {
		b.CodeLength = 8
	}
	if b.CodePreset == "" {
		b.CodePreset = VoucherDefaultPreset
	}
	alphabet := VoucherCodeAlphabet(b.CodePreset)
	if alphabet == "" {
		return errors.New("choose a known code character set")
	}
	if b.CodeLength < 4 || b.CodeLength > 10 {
		return errors.New("choose a code length from 4 to 10")
	}
	if len(b.CodePrefix) > 6 || strings.Trim(b.CodePrefix, voucherLetters+strings.ToLower(voucherLetters)+voucherDigits) != "" {
		return errors.New("prefix must be up to 6 letters or digits")
	}
	size := VoucherCodeSpaceSize(b.CodePreset)
	space := int64(1)
	for i := 0; i < b.CodeLength; i++ {
		space *= size
	}
	if int64(count)*100 > space {
		return errors.New("batch count exceeds 1% of the possible codes; choose fewer Vouchers or a longer code")
	}
	return nil
}

const voucherSchema = `
CREATE TABLE IF NOT EXISTS voucher_batches (
 id TEXT PRIMARY KEY, seconds INTEGER NOT NULL CHECK(seconds > 0), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voucher_prices (
 batch_id TEXT PRIMARY KEY REFERENCES voucher_batches(id), pesos INTEGER NOT NULL CHECK(pesos >= 0)
);
CREATE TABLE IF NOT EXISTS time_credits (
 id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, router_password TEXT NOT NULL,
 bought_seconds INTEGER NOT NULL CHECK(bought_seconds >= 0), mac TEXT NOT NULL, ip TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vouchers (
 code TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES voucher_batches(id),
 time_credit_id TEXT REFERENCES time_credits(id), redeemed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
 time_credit_id TEXT NOT NULL REFERENCES time_credits(id),
 before_seconds INTEGER NOT NULL, after_seconds INTEGER NOT NULL, source TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
`

type VoucherBatch struct {
	ID              string `json:"id"`
	Seller          string `json:"seller"`
	CodePreset      string `json:"code_preset"`
	CodeLength      int    `json:"code_length"`
	CodePrefix      string `json:"code_prefix"`
	Number          int64  `json:"number"`
	TierLabel       string `json:"tier_label"`
	ValiditySeconds int64  `json:"validity_seconds"`
	PauseLimit      int    `json:"pause_limit"`
	Count           int    `json:"count"`
	RedeemedCount   int    `json:"redeemed_count"`
	Seconds         int64  `json:"seconds"`
	CreatedAt       string `json:"created_at"`
	Pesos           *int64 `json:"pesos"`
}

type Voucher struct {
	Code         string `json:"code"`
	BatchID      string `json:"batch_id"`
	TimeCreditID string `json:"time_credit_id"`
	RedeemedAt   string `json:"redeemed_at"`
}

func randomToken() (string, error) {
	value := make([]byte, 32)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(value), nil
}

// Resume Codes keep the original format independently of Voucher settings.
func randomCode() (string, error) {
	return randomVoucherCode(VoucherAlphabet, 8)
}

// Rejection sampling keeps CSPRNG characters uniform for each alphabet.
func randomVoucherCode(alphabet string, length int) (string, error) {
	code := make([]byte, 0, length)
	for len(code) < length {
		var value [1]byte
		if _, err := rand.Read(value[:]); err != nil {
			return "", err
		}
		if int(value[0]) < 256-256%len(alphabet) {
			code = append(code, alphabet[int(value[0])%len(alphabet)])
		}
	}
	return string(code), nil
}

func (l *Ledger) CreateVoucherBatch(batch VoucherBatch, count int) (VoucherBatch, []Voucher, error) {
	batch.Seller = strings.TrimSpace(batch.Seller)
	if batch.Seller == "Shop" {
		batch.Seller = ""
	}
	if err := batch.ValidateCodeFormat(count); err != nil {
		return VoucherBatch{}, nil, err
	}
	if batch.Pesos == nil || *batch.Pesos < 0 || *batch.Pesos > 1000000 {
		return VoucherBatch{}, nil, errors.New("choose 0–1000000 pesos; price is required")
	}
	if batch.Seconds < 1 || batch.Seconds > 31536000 || count < 1 || count > 1000 {
		return VoucherBatch{}, nil, errors.New("choose 1–31536000 seconds and 1–1000 Vouchers")
	}

	id, err := randomToken()
	if err != nil {
		return VoucherBatch{}, nil, err
	}
	batch.ID, batch.CreatedAt = id, time.Now().UTC().Format(time.RFC3339)
	tx, err := l.db.Begin()
	if err != nil {
		return batch, nil, err
	}
	defer tx.Rollback()
	// Custom batches snapshot the Shop's Voucher settings in the mint transaction.
	if batch.TierLabel == "" {
		batch.ValiditySeconds, err = siteValiditySeconds(tx)
		if err != nil {
			return batch, nil, err
		}
		pauses, err := pauseSettings(tx)
		if err != nil {
			return batch, nil, err
		}
		batch.PauseLimit = pauses.PauseLimit
	}
	if batch.ValiditySeconds < 1 || batch.ValiditySeconds > MaxValiditySeconds || batch.PauseLimit < 0 || batch.PauseLimit > 1000 {
		return batch, nil, errors.New("invalid Voucher Validity or Pause Limit")
	}
	if err = tx.QueryRow(`SELECT COALESCE(MAX(number), 0) + 1 FROM (
 SELECT number FROM voucher_batches UNION ALL SELECT batch_number FROM voucher_batch_deletions
 )`).Scan(&batch.Number); err != nil {
		return batch, nil, err
	}
	if _, err = tx.Exec(`INSERT INTO voucher_batches (id, seconds, created_at, number, tier_label, validity_seconds, pause_limit, code_preset, code_length, code_prefix, seller) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''))`, batch.ID, batch.Seconds, batch.CreatedAt, batch.Number, batch.TierLabel, batch.ValiditySeconds, batch.PauseLimit, batch.CodePreset, batch.CodeLength, batch.CodePrefix, batch.Seller); err != nil {
		return batch, nil, err
	}
	if _, err = tx.Exec("INSERT INTO voucher_prices (batch_id, pesos) VALUES (?, ?)", batch.ID, *batch.Pesos); err != nil {
		return batch, nil, err
	}

	vouchers := make([]Voucher, 0, count)
	for len(vouchers) < count {
		code, err := randomVoucherCode(VoucherCodeAlphabet(batch.CodePreset), batch.CodeLength)
		if err != nil {
			return batch, nil, err
		}
		code = batch.CodePrefix + code
		result, err := tx.Exec("INSERT INTO vouchers (code, batch_id) VALUES (?, ?) ON CONFLICT DO NOTHING", code, batch.ID)
		if err != nil {
			return batch, nil, err
		}
		inserted, err := result.RowsAffected()
		if err != nil {
			return batch, nil, err
		}
		if inserted == 1 {
			vouchers = append(vouchers, Voucher{Code: code, BatchID: batch.ID})
		}
	}
	return batch, vouchers, tx.Commit()
}

const voucherBatchSelect = `SELECT b.id, b.seconds, b.created_at, p.pesos, b.number, COALESCE(b.tier_label, ''), b.validity_seconds, b.pause_limit, b.code_preset, b.code_length, b.code_prefix, COALESCE(b.seller, '') FROM voucher_batches b
		LEFT JOIN voucher_prices p ON p.batch_id = b.id`

func scanVoucherBatch(row interface{ Scan(...any) error }) (VoucherBatch, error) {
	var batch VoucherBatch
	err := row.Scan(&batch.ID, &batch.Seconds, &batch.CreatedAt, &batch.Pesos, &batch.Number, &batch.TierLabel, &batch.ValiditySeconds, &batch.PauseLimit, &batch.CodePreset, &batch.CodeLength, &batch.CodePrefix, &batch.Seller)
	return batch, err
}

func (l *Ledger) VoucherBatch(id string) (VoucherBatch, []Voucher, error) {
	batch, err := scanVoucherBatch(l.db.QueryRow(voucherBatchSelect+" WHERE b.id = ?", id))
	if err != nil {
		return batch, nil, err
	}
	rows, err := l.db.Query("SELECT code, COALESCE(time_credit_id, ''), COALESCE(redeemed_at, '') FROM vouchers WHERE batch_id = ? ORDER BY code", id)
	if err != nil {
		return batch, nil, err
	}
	defer rows.Close()
	vouchers := []Voucher{}
	for rows.Next() {
		voucher := Voucher{BatchID: id}
		if err := rows.Scan(&voucher.Code, &voucher.TimeCreditID, &voucher.RedeemedAt); err != nil {
			return batch, nil, err
		}
		vouchers = append(vouchers, voucher)
	}
	return batch, vouchers, rows.Err()
}

func (l *Ledger) initializeInstallKey() error {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return err
	}
	_, err := l.db.Exec("INSERT INTO settings (key, value) VALUES ('portal_signing_key', ?) ON CONFLICT(key) DO NOTHING", key)
	return err
}

// InstallKey survives restarts and is never exposed by public state readback.
func (l *Ledger) InstallKey() ([]byte, error) {
	var key []byte
	if err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'portal_signing_key'").Scan(&key); err != nil {
		return nil, err
	}
	return key, nil
}

type TimeCredit struct {
	ID             string `json:"id"`
	Token          string `json:"-"`
	RouterPassword string `json:"-"`
	BoughtSeconds  int64  `json:"bought_seconds"`
	MAC            string `json:"mac"`
	IP             string `json:"ip"`
	PointsMilli    int64  `json:"points_milli"` // Points in thousandths (ADR 0006)
}

// RemainingSeconds derives time from a fresh Router usage read; it is never stored.
func (tc TimeCredit) RemainingSeconds(used int64) int64 {
	return max(int64(0), tc.BoughtSeconds-used)
}

const timeCreditColumns = "id, token, router_password, bought_seconds, mac, ip, points_milli"

func scanTimeCredit(row *sql.Row) (TimeCredit, error) {
	var tc TimeCredit
	err := row.Scan(&tc.ID, &tc.Token, &tc.RouterPassword, &tc.BoughtSeconds, &tc.MAC, &tc.IP, &tc.PointsMilli)
	return tc, err
}

func (l *Ledger) TimeCreditByToken(token string) (TimeCredit, error) {
	return scanTimeCredit(l.db.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE token = ?", token))
}

// TimeCreditByDevice is the Device's valid Time Credit or, failing that, an
// expired one still holding Points: those outlive Validity (ADR 0006), and the
// Device's next purchase carries them to a replacement.
func (l *Ledger) TimeCreditByDevice(mac, ip string) (TimeCredit, error) {
	return scanTimeCredit(l.db.QueryRow(`SELECT `+timeCreditColumns+` FROM time_credits
	 JOIN time_credit_validity v ON v.time_credit_id = time_credits.id
	 WHERE mac = ? AND ip = ? AND ((v.expired_at = 0 AND v.valid_until > unixepoch()) OR points_milli > 0)
	 ORDER BY v.expired_at = 0 AND v.valid_until > unixepoch() DESC, v.valid_until DESC LIMIT 1`, mac, ip))
}

var ErrVoucherUnknown = errors.New("Voucher not found at this site")
var ErrVoucherUsed = errors.New("Voucher already used")

// RedeemVoucher consumes the code, credits bought time, resets the Top-up Pause
// count, and queues its audit and credit intent in one transaction. The single
// connection serializes concurrent writers. A Voucher earns no Points, but one
// redeemed on an expired Time Credit carries its Points to the replacement.
func (l *Ledger) RedeemVoucher(code, mac, ip, existingID string) (TimeCredit, error) {
	tx, err := l.db.Begin()
	if err != nil {
		return TimeCredit{}, err
	}
	defer tx.Rollback()
	var seconds, validity int64
	var pauseLimit int
	var redeemed sql.NullString
	err = tx.QueryRow(`SELECT b.seconds, v.time_credit_id, b.validity_seconds, b.pause_limit, v.code FROM vouchers v JOIN voucher_batches b ON b.id = v.batch_id WHERE v.code = ? COLLATE NOCASE`, code).Scan(&seconds, &redeemed, &validity, &pauseLimit, &code)
	if errors.Is(err, sql.ErrNoRows) {
		return TimeCredit{}, ErrVoucherUnknown
	}
	if err != nil {
		return TimeCredit{}, err
	}
	if redeemed.Valid {
		return TimeCredit{}, ErrVoucherUsed
	}
	// Voucher Batch limits are fixed at mint time (ADR 0008).
	carryFrom := ""
	if existingID != "" {
		if err := ExtendValidity(tx, existingID, validity); errors.Is(err, ErrTimeCreditExpired) {
			// Forfeited time never carries into a new purchase; Points do.
			carryFrom, existingID = existingID, ""
		} else if err != nil {
			return TimeCredit{}, err
		}
	}
	var tc TimeCredit
	before := int64(0)
	if existingID != "" {
		tc, err = scanTimeCredit(tx.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = ?", existingID))
		if err != nil {
			return tc, err
		}
		before = tc.BoughtSeconds
		if before > 315360000-seconds {
			return tc, errors.New("Time Credit purchase limit reached")
		}
		tc.BoughtSeconds += seconds
		if _, err = tx.Exec("UPDATE time_credits SET bought_seconds = ? WHERE id = ?", tc.BoughtSeconds, tc.ID); err != nil {
			return tc, err
		}
	} else {
		tc = TimeCredit{BoughtSeconds: seconds, MAC: mac, IP: ip}
		tc.ID, err = randomToken()
		if err != nil {
			return tc, err
		}
		tc.Token, err = randomToken()
		if err != nil {
			return tc, err
		}
		tc.RouterPassword, err = randomToken()
		if err != nil {
			return tc, err
		}
		_, err = tx.Exec("INSERT INTO time_credits (id, token, router_password, bought_seconds, mac, ip) VALUES (?, ?, ?, ?, ?, ?)", tc.ID, tc.Token, tc.RouterPassword, tc.BoughtSeconds, tc.MAC, tc.IP)
		if err == nil {
			err = insertResumeCode(tx, tc.ID)
		}
	}
	if err != nil {
		return tc, err
	}
	if existingID == "" {
		if err := ExtendValidity(tx, tc.ID, validity); err != nil {
			return tc, err
		}
	}
	if carryFrom != "" {
		if err := carryPoints(tx, carryFrom, tc.ID); err != nil {
			return tc, err
		}
		if err := tx.QueryRow("SELECT points_milli FROM time_credits WHERE id = ?", tc.ID).Scan(&tc.PointsMilli); err != nil {
			return tc, err
		}
	}
	if err := resetPauses(tx, tc.ID, pauseLimit); err != nil {
		return tc, err
	}

	now := time.Now().UTC().Format(time.RFC3339Nano)
	result, err := tx.Exec(`UPDATE vouchers SET time_credit_id = ?, redeemed_at = ?, redeemed_mac = ?, redeemed_ip = ?,
	 valid_until = (SELECT valid_until FROM time_credit_validity WHERE time_credit_id = ?)
	 WHERE code = ? AND time_credit_id IS NULL`, tc.ID, now, mac, ip, tc.ID, code)
	if err != nil {
		return tc, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return tc, err
	}
	if changed != 1 {
		return tc, ErrVoucherUsed
	}
	_, err = tx.Exec(`INSERT INTO audit (at, actor, reason, time_credit_id, before_seconds, after_seconds, source) VALUES (?, ?, 'voucher-redemption', ?, ?, ?, ?)`, now, "device:"+mac, tc.ID, before, tc.BoughtSeconds, code)
	if err != nil {
		return tc, err
	}
	if err := QueueCreditIntent(tx, tc.ID, tc.BoughtSeconds); err != nil {
		return tc, err
	}
	return tc, tx.Commit()
}

type Audit struct {
	ID            int64  `json:"id"`
	At            string `json:"at"`
	Actor         string `json:"actor"`
	Reason        string `json:"reason"`
	TimeCreditID  string `json:"time_credit_id"`
	BeforeSeconds int64  `json:"before_seconds"`
	AfterSeconds  int64  `json:"after_seconds"`
	Source        string `json:"source"`
}

type PurchaseState struct {
	Batches     []VoucherBatch `json:"batches"`
	Vouchers    []Voucher      `json:"vouchers"`
	TimeCredits []TimeCredit   `json:"time_credits"`
	Audit       []Audit        `json:"audit"`
	Rebinds     []Rebind       `json:"rebinds"`
	Points      []PointsEntry  `json:"points_journal"`
}

func (l *Ledger) PurchaseState() (PurchaseState, error) {
	state := PurchaseState{Batches: []VoucherBatch{}, Vouchers: []Voucher{}, TimeCredits: []TimeCredit{}, Audit: []Audit{}}
	var err error
	if state.Rebinds, err = l.rebinds(); err != nil {
		return state, err
	}
	if state.Points, err = l.pointsJournal(); err != nil {
		return state, err
	}
	// Close each result before the next query: the ledger has one connection.
	rows, err := l.db.Query(voucherBatchSelect + " ORDER BY b.created_at, b.id")
	if err != nil {
		return state, err
	}
	for rows.Next() {
		b, err := scanVoucherBatch(rows)
		if err != nil {
			rows.Close()
			return state, err
		}
		state.Batches = append(state.Batches, b)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return state, err
	}
	rows, err = l.db.Query("SELECT code, batch_id, COALESCE(time_credit_id, ''), COALESCE(redeemed_at, '') FROM vouchers ORDER BY code")
	if err != nil {
		return state, err
	}
	for rows.Next() {
		var v Voucher
		if err = rows.Scan(&v.Code, &v.BatchID, &v.TimeCreditID, &v.RedeemedAt); err != nil {
			rows.Close()
			return state, err
		}
		state.Vouchers = append(state.Vouchers, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return state, err
	}
	rows, err = l.db.Query("SELECT " + timeCreditColumns + " FROM time_credits ORDER BY id")
	if err != nil {
		return state, err
	}
	for rows.Next() {
		var tc TimeCredit
		if err = rows.Scan(&tc.ID, &tc.Token, &tc.RouterPassword, &tc.BoughtSeconds, &tc.MAC, &tc.IP, &tc.PointsMilli); err != nil {
			rows.Close()
			return state, err
		}
		state.TimeCredits = append(state.TimeCredits, tc)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return state, err
	}
	rows, err = l.db.Query("SELECT id, at, actor, reason, time_credit_id, before_seconds, after_seconds, source FROM audit ORDER BY id")
	if err != nil {
		return state, err
	}
	defer rows.Close()
	for rows.Next() {
		var a Audit
		if err := rows.Scan(&a.ID, &a.At, &a.Actor, &a.Reason, &a.TimeCreditID, &a.BeforeSeconds, &a.AfterSeconds, &a.Source); err != nil {
			return state, fmt.Errorf("read audit: %w", err)
		}
		state.Audit = append(state.Audit, a)
	}
	return state, rows.Err()
}
