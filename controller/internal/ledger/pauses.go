package ledger

import (
	"database/sql"
	"encoding/json"
	"errors"
)

const pauseSchema = `
CREATE TABLE IF NOT EXISTS time_credit_pauses (
 time_credit_id TEXT PRIMARY KEY REFERENCES time_credits(id),
 tapped_count INTEGER NOT NULL DEFAULT 0 CHECK(tapped_count >= 0),
 tapped INTEGER NOT NULL DEFAULT 0 CHECK(tapped IN (0, 1))
);
`

type PauseSettings struct {
	PauseLimit  int  `json:"pause_limit"`
	IdleEnabled bool `json:"idle_enabled"`
	IdleSeconds int  `json:"idle_seconds"`
}

func (l *Ledger) PauseSettings() (PauseSettings, error) {
	return pauseSettings(l.db)
}

// pauseSettings reads the site settings, inside a Top-up's transaction too.
func pauseSettings(db queryer) (PauseSettings, error) {
	settings := PauseSettings{PauseLimit: 3, IdleSeconds: 120}
	var encoded []byte
	err := db.QueryRow("SELECT value FROM settings WHERE key = 'pause_settings'").Scan(&encoded)
	if errors.Is(err, sql.ErrNoRows) {
		return settings, nil
	}
	if err != nil {
		return settings, err
	}
	err = json.Unmarshal(encoded, &settings)
	return settings, err
}

func (l *Ledger) SavePauseSettings(settings PauseSettings) error {
	if settings.PauseLimit < 0 || settings.PauseLimit > 1000 || settings.IdleSeconds < 1 || settings.IdleSeconds > 86400 {
		return errors.New("choose a Pause Limit of 0–1000 and an idle period of 1–86400 seconds")
	}
	encoded, err := json.Marshal(settings)
	if err != nil {
		return err
	}
	_, err = l.db.Exec("INSERT INTO settings (key, value) VALUES ('pause_settings', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", encoded)
	return err
}

// PauseState carries the Pause Limit set by the Time Credit's latest Top-up.
// A stored NULL means the live site value: only Time Credits not topped up
// since every Top-up began storing its limit have one.
type PauseState struct {
	TimeCreditID string `json:"time_credit_id"`
	TappedCount  int    `json:"tapped_count"`
	Tapped       bool   `json:"tapped"`
	PauseLimit   int    `json:"pause_limit"`
}

func (l *Ledger) PauseState(id string) (PauseState, error) {
	settings, err := l.PauseSettings()
	if err != nil {
		return PauseState{}, err
	}
	state := PauseState{TimeCreditID: id, PauseLimit: settings.PauseLimit}
	var limit sql.NullInt64
	err = l.db.QueryRow("SELECT tapped_count, tapped, pause_limit FROM time_credit_pauses WHERE time_credit_id = ?", id).Scan(&state.TappedCount, &state.Tapped, &limit)
	if errors.Is(err, sql.ErrNoRows) {
		return state, nil
	}
	if limit.Valid {
		state.PauseLimit = int(limit.Int64)
	}
	return state, err
}

// resetPauses is part of every Top-up: it restores the Pauses and stores the
// Top-up's Pause Limit, even when that lowers it. Buying time is not explicit
// consent to Resume, so a tapped Pause stays tapped.
func resetPauses(tx *sql.Tx, id string, limit int) error {
	_, err := tx.Exec(`INSERT INTO time_credit_pauses (time_credit_id, pause_limit) VALUES (?, ?)
	 ON CONFLICT(time_credit_id) DO UPDATE SET tapped_count = 0, pause_limit = excluded.pause_limit`, id, limit)
	return err
}

var ErrPauseLimit = errors.New("Pause Limit reached. You have no Pauses left")

// Record the tapped intent before removing the Router session. A retry of a
// paused Time Credit can finish the removal without consuming another Pause.
// The stored Pause Limit applies; siteLimit covers Time Credits without one.
func (l *Ledger) TakePause(id string, siteLimit int) error {
	result, err := l.db.Exec(`INSERT INTO time_credit_pauses (time_credit_id, tapped_count, tapped)
	 SELECT ?, 1, 1 WHERE COALESCE((SELECT pause_limit FROM time_credit_pauses WHERE time_credit_id = ?), ?) > 0
	 ON CONFLICT(time_credit_id) DO UPDATE SET tapped_count = tapped_count + 1, tapped = 1
	 WHERE tapped = 0 AND tapped_count < COALESCE(pause_limit, ?)`, id, id, siteLimit, siteLimit)
	if err != nil {
		return err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if changed == 0 {
		state, err := l.PauseState(id)
		if err != nil {
			return err
		}
		if !state.Tapped {
			return ErrPauseLimit
		}
	}
	return nil
}

func (l *Ledger) ResumePause(id string) error {
	_, err := l.db.Exec("UPDATE time_credit_pauses SET tapped = 0 WHERE time_credit_id = ?", id)
	return err
}

func (l *Ledger) Pauses() ([]PauseState, error) {
	settings, err := l.PauseSettings()
	if err != nil {
		return nil, err
	}
	rows, err := l.db.Query("SELECT time_credit_id, tapped_count, tapped, COALESCE(pause_limit, ?) FROM time_credit_pauses ORDER BY time_credit_id", settings.PauseLimit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	states := []PauseState{}
	for rows.Next() {
		var state PauseState
		if err := rows.Scan(&state.TimeCreditID, &state.TappedCount, &state.Tapped, &state.PauseLimit); err != nil {
			return nil, err
		}
		states = append(states, state)
	}
	return states, rows.Err()
}
