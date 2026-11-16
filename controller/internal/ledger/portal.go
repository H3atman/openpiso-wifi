package ledger

import (
	"database/sql"
	"errors"
	"time"
)

// A Custom Portal lives in three slots: the Operator's draft, the published
// version Devices get, and the previous published version Revert swaps back to.
// The Default Portal is embedded in the binary and never stored; an empty
// published slot means Devices get it. An empty or NULL logo or cover means the built-in one.
const portalSchema = `CREATE TABLE IF NOT EXISTS portal_versions (
 slot TEXT PRIMARY KEY CHECK(slot IN ('draft', 'published', 'previous')),
 version INTEGER NOT NULL,
 index_html TEXT NOT NULL,
 portal_css TEXT NOT NULL,
 custom_js TEXT NOT NULL,
 logo BLOB,
 cover BLOB,
 saved_at INTEGER NOT NULL
)`

type PortalSlot string

const (
	PortalDraft     PortalSlot = "draft"
	PortalPublished PortalSlot = "published"
	PortalPrevious  PortalSlot = "previous"
)

var (
	ErrNoPortalDraft    = errors.New("save a draft before publishing")
	ErrNoPortalPrevious = errors.New("there is no previous version to revert to")
)

// PortalFiles are the Operator-editable files; core.js is never among them.
// Logo and Cover are WebP images, empty for the built-in ones.
type PortalFiles struct {
	IndexHTML string
	PortalCSS string
	CustomJS  string
	Logo      []byte
	Cover     []byte
}

// PortalVersion is one saved draft. Version numbers count up with every save,
// so the same number in two slots is the same content.
type PortalVersion struct {
	Version int64       `json:"version"`
	SavedAt int64       `json:"saved_at"` // Unix seconds
	Files   PortalFiles `json:"-"`
}

// PortalState is what /admin/state shows of the slots; nil is an empty slot.
type PortalState struct {
	Draft     *PortalVersion `json:"draft"`
	Published *PortalVersion `json:"published"`
	Previous  *PortalVersion `json:"previous"`
}

type queryer interface {
	QueryRow(query string, args ...any) *sql.Row
}

func portalVersion(db queryer, slot PortalSlot) (*PortalVersion, error) {
	v := &PortalVersion{}
	err := db.QueryRow("SELECT version, saved_at, index_html, portal_css, custom_js, logo, cover FROM portal_versions WHERE slot = ?", slot).
		Scan(&v.Version, &v.SavedAt, &v.Files.IndexHTML, &v.Files.PortalCSS, &v.Files.CustomJS, &v.Files.Logo, &v.Files.Cover)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return v, err
}

// putPortal stores v in slot, or empties the slot when v is nil.
func putPortal(tx *sql.Tx, slot PortalSlot, v *PortalVersion) error {
	if v == nil {
		_, err := tx.Exec("DELETE FROM portal_versions WHERE slot = ?", slot)
		return err
	}
	_, err := tx.Exec(`INSERT INTO portal_versions (slot, version, index_html, portal_css, custom_js, logo, cover, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
	 ON CONFLICT(slot) DO UPDATE SET version = excluded.version, index_html = excluded.index_html,
	 portal_css = excluded.portal_css, custom_js = excluded.custom_js, logo = excluded.logo, cover = excluded.cover,
	 saved_at = excluded.saved_at`,
		slot, v.Version, v.Files.IndexHTML, v.Files.PortalCSS, v.Files.CustomJS, v.Files.Logo, v.Files.Cover, v.SavedAt)
	return err
}

// PortalVersion reads one slot; nil if it is empty.
func (l *Ledger) PortalVersion(slot PortalSlot) (*PortalVersion, error) {
	return portalVersion(l.db, slot)
}

func (l *Ledger) PortalState() (PortalState, error) {
	draft, err := portalVersion(l.db, PortalDraft)
	if err != nil {
		return PortalState{}, err
	}
	published, err := portalVersion(l.db, PortalPublished)
	if err != nil {
		return PortalState{}, err
	}
	previous, err := portalVersion(l.db, PortalPrevious)
	return PortalState{draft, published, previous}, err
}

// SavePortalDraft replaces the draft with files, which the caller has already
// passed through the save check and the image check, and returns its new version number.
func (l *Ledger) SavePortalDraft(files PortalFiles) (int64, error) {
	v := &PortalVersion{SavedAt: time.Now().Unix(), Files: files}
	err := l.portalChange(func(tx *sql.Tx, _ map[PortalSlot]*PortalVersion) error {
		if err := tx.QueryRow("SELECT COALESCE(MAX(version), 0) + 1 FROM portal_versions").Scan(&v.Version); err != nil {
			return err
		}
		return putPortal(tx, PortalDraft, v)
	})
	return v.Version, err
}

// PublishPortal makes the draft what Devices get and keeps the published
// version as previous. Republishing the published draft changes nothing.
func (l *Ledger) PublishPortal() error {
	return l.portalChange(func(tx *sql.Tx, slots map[PortalSlot]*PortalVersion) error {
		draft, published := slots[PortalDraft], slots[PortalPublished]
		if draft == nil {
			return ErrNoPortalDraft
		}
		if published != nil && published.Version == draft.Version {
			return nil
		}
		if published != nil {
			if err := putPortal(tx, PortalPrevious, published); err != nil {
				return err
			}
		}
		return putPortal(tx, PortalPublished, draft)
	})
}

// RevertPortal swaps the published and previous versions. After a Reset to
// default the previous version is the one Reset removed, so Revert undoes it.
func (l *Ledger) RevertPortal() error {
	return l.portalChange(func(tx *sql.Tx, slots map[PortalSlot]*PortalVersion) error {
		if slots[PortalPrevious] == nil {
			return ErrNoPortalPrevious
		}
		if err := putPortal(tx, PortalPublished, slots[PortalPrevious]); err != nil {
			return err
		}
		return putPortal(tx, PortalPrevious, slots[PortalPublished])
	})
}

// ResetPortal empties the published slot so Devices get the Default Portal,
// keeping what was published as previous.
func (l *Ledger) ResetPortal() error {
	return l.portalChange(func(tx *sql.Tx, slots map[PortalSlot]*PortalVersion) error {
		if slots[PortalPublished] == nil {
			return nil
		}
		if err := putPortal(tx, PortalPrevious, slots[PortalPublished]); err != nil {
			return err
		}
		return putPortal(tx, PortalPublished, nil)
	})
}

// portalChange runs change in one transaction, handing it every slot as read inside it.
func (l *Ledger) portalChange(change func(*sql.Tx, map[PortalSlot]*PortalVersion) error) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	slots := map[PortalSlot]*PortalVersion{}
	for _, slot := range []PortalSlot{PortalDraft, PortalPublished, PortalPrevious} {
		if slots[slot], err = portalVersion(tx, slot); err != nil {
			return err
		}
	}
	if err := change(tx, slots); err != nil {
		return err
	}
	return tx.Commit()
}
