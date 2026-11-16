package ledger

import (
	"database/sql"
	"errors"
	"slices"
)

// timeCreditProfileSchema holds each Time Credit's HotSpot profile family: the
// profile of the Vendo of its latest coin Top-up. No row or ” is the base family.
const timeCreditProfileSchema = `
CREATE TABLE IF NOT EXISTS time_credit_profiles (
 time_credit_id TEXT PRIMARY KEY REFERENCES time_credits(id),
 hotspot_profile TEXT NOT NULL
);
`

// setCoinTopUpProfile belongs in a coin Top-up's transaction: the Time Credit
// takes the HotSpot profile its Coin Window's Vendo has now.
func setCoinTopUpProfile(tx *sql.Tx, timeCreditID string, windowID uint32) error {
	_, err := tx.Exec(`INSERT INTO time_credit_profiles (time_credit_id, hotspot_profile)
	 SELECT ?, v.hotspot_profile FROM coin_windows w JOIN vendos v ON v.id = w.vendo_id WHERE w.id = ?
	 ON CONFLICT(time_credit_id) DO UPDATE SET hotspot_profile = excluded.hotspot_profile`, timeCreditID, windowID)
	return err
}

// TimeCreditProfile is the Time Credit's HotSpot profile family, ” for base.
func (l *Ledger) TimeCreditProfile(id string) (string, error) {
	var profile string
	err := l.db.QueryRow("SELECT hotspot_profile FROM time_credit_profiles WHERE time_credit_id = ?", id).Scan(&profile)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return profile, err
}

// ProfileUse is which HotSpot profiles are in use, for the Router's copies.
type ProfileUse struct {
	TimeCredits map[string]string // Time Credit ID → its family, for those still on the Router
	Profiles    []string          // every profile a Vendo or such a Time Credit uses, once
}

// HotspotProfileUse reads ProfileUse. A Vendo uses its profile until unpaired;
// a Time Credit until Validity removes its Router user.
func (l *Ledger) HotspotProfileUse() (ProfileUse, error) {
	use := ProfileUse{TimeCredits: map[string]string{}, Profiles: []string{}}
	rows, err := l.db.Query(`SELECT p.time_credit_id, p.hotspot_profile FROM time_credit_profiles p
	 WHERE p.hotspot_profile != '' AND NOT EXISTS (SELECT 1 FROM time_credit_validity v
	  WHERE v.time_credit_id = p.time_credit_id AND v.router_removed = 1)`)
	if err != nil {
		return use, err
	}
	defer rows.Close()
	for rows.Next() {
		var id, profile string
		if err := rows.Scan(&id, &profile); err != nil {
			return use, err
		}
		use.TimeCredits[id] = profile
	}
	if err := rows.Err(); err != nil {
		return use, err
	}
	rows, err = l.db.Query("SELECT hotspot_profile FROM vendos WHERE status != 'unpaired' AND hotspot_profile != ''")
	if err != nil {
		return use, err
	}
	defer rows.Close()
	for rows.Next() {
		var profile string
		if err := rows.Scan(&profile); err != nil {
			return use, err
		}
		use.Profiles = append(use.Profiles, profile)
	}
	for _, profile := range use.TimeCredits {
		use.Profiles = append(use.Profiles, profile)
	}
	slices.Sort(use.Profiles)
	use.Profiles = slices.Compact(use.Profiles)
	return use, rows.Err()
}
