package ledger

import (
	"database/sql"
	"errors"
	"time"
)

const creditIntentSchema = `
CREATE TABLE IF NOT EXISTS credit_intents (
 time_credit_id TEXT PRIMARY KEY REFERENCES time_credits(id),
 bought_seconds INTEGER NOT NULL CHECK(bought_seconds > 0),
 revision INTEGER NOT NULL, applied_revision INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
INSERT INTO credit_intents (time_credit_id, bought_seconds, revision, updated_at)
 SELECT id, bought_seconds, 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now') FROM time_credits
 WHERE bought_seconds > 0 ON CONFLICT(time_credit_id) DO NOTHING;
`

type CreditIntent struct {
	TimeCreditID  string `json:"time_credit_id"`
	BoughtSeconds int64  `json:"bought_seconds"`
	Revision      int64  `json:"revision"`
	Pending       bool   `json:"pending"`
	Error         string `json:"error"`
	UpdatedAt     string `json:"updated_at"`
}

// QueueCreditIntent belongs in the same transaction as a bought-time change
// and its audit, or a Rebind that supersedes an interrupted projection.
// The target is absolute, never an increment or remaining time.
// A newer revision supersedes any interrupted projection for this Time Credit.
func QueueCreditIntent(tx *sql.Tx, id string, boughtSeconds int64) error {
	_, err := tx.Exec(`INSERT INTO credit_intents (time_credit_id, bought_seconds, revision, updated_at)
 VALUES (?, ?, 1, ?) ON CONFLICT(time_credit_id) DO UPDATE SET
 bought_seconds = excluded.bought_seconds, revision = credit_intents.revision + 1,
 error = '', updated_at = excluded.updated_at`, id, boughtSeconds, time.Now().UTC().Format(time.RFC3339))
	return err
}

func (l *Ledger) CreditIntents() ([]CreditIntent, error) {
	rows, err := l.db.Query(`SELECT time_credit_id, bought_seconds, revision, revision > applied_revision, error, updated_at FROM credit_intents ORDER BY time_credit_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	intents := []CreditIntent{}
	for rows.Next() {
		var intent CreditIntent
		if err := rows.Scan(&intent.TimeCreditID, &intent.BoughtSeconds, &intent.Revision, &intent.Pending, &intent.Error, &intent.UpdatedAt); err != nil {
			return nil, err
		}
		intents = append(intents, intent)
	}
	return intents, rows.Err()
}

func (l *Ledger) CreditIntent(id string) (CreditIntent, error) {
	intent := CreditIntent{TimeCreditID: id}
	err := l.db.QueryRow(`SELECT bought_seconds, revision, revision > applied_revision, error, updated_at
 FROM credit_intents WHERE time_credit_id = ?`, id).Scan(&intent.BoughtSeconds, &intent.Revision, &intent.Pending, &intent.Error, &intent.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return intent, nil
	}
	return intent, err
}

// FinishCreditIntent never acknowledges a newer purchase on behalf of an older
// Router write. Error text is deliberately fixed: Router errors may hold secrets.
func (l *Ledger) FinishCreditIntent(intent CreditIntent, projectionErr error) error {
	if !intent.Pending {
		return nil
	}
	if projectionErr != nil {
		_, err := l.db.Exec(`UPDATE credit_intents SET error = 'Router projection failed; retry pending'
 WHERE time_credit_id = ? AND revision = ? AND applied_revision < revision AND error = ''`, intent.TimeCreditID, intent.Revision)
		return err
	}
	_, err := l.db.Exec(`UPDATE credit_intents SET applied_revision = revision, error = '', updated_at = ?
 WHERE time_credit_id = ? AND revision = ? AND applied_revision < revision`, time.Now().UTC().Format(time.RFC3339), intent.TimeCreditID, intent.Revision)
	return err
}

func (l *Ledger) TimeCreditByID(id string) (TimeCredit, error) {
	return scanTimeCredit(l.db.QueryRow("SELECT "+timeCreditColumns+" FROM time_credits WHERE id = ?", id))
}
