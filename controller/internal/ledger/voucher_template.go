package ledger

import (
	"database/sql"
	"errors"
	"time"
)

// A Voucher template lives in three slots: the Operator's draft, the published
// version Print sheets use, and the previous published version Revert swaps back to.
// The default Voucher template is embedded in the binary and never stored; an empty
// published slot means Print sheets use it. Version zero represents the default in previous.
const voucherTemplateSchema = `INSERT INTO settings (key, value) VALUES ('shop_name', 'OpenPiso WiFi') ON CONFLICT(key) DO NOTHING;
CREATE TABLE IF NOT EXISTS voucher_versions (
 slot TEXT PRIMARY KEY CHECK(slot IN ('draft', 'published', 'previous')),
 version INTEGER NOT NULL,
 voucher_html TEXT NOT NULL,
 voucher_css TEXT NOT NULL,
 custom_js TEXT NOT NULL,
 saved_at INTEGER NOT NULL
)`

type VoucherSlot string

const (
	VoucherDraft     VoucherSlot = "draft"
	VoucherPublished VoucherSlot = "published"
	VoucherPrevious  VoucherSlot = "previous"
)

var (
	ErrNoVoucherDraft    = errors.New("save a draft before publishing")
	ErrNoVoucherPrevious = errors.New("there is no previous version to revert to")
)

// VoucherFiles are the editable template files; voucher.js stays in the binary.
type VoucherFiles struct {
	VoucherHTML string
	VoucherCSS  string
	CustomJS    string
}

// VoucherVersion is one saved draft. Version numbers count up with every save,
// so the same number in two slots is the same content.
type VoucherVersion struct {
	Version int64        `json:"version"`
	SavedAt int64        `json:"saved_at"` // Unix seconds
	Files   VoucherFiles `json:"-"`
}

// VoucherState describes the editor slots; nil is an empty slot.
type VoucherState struct {
	Draft     *VoucherVersion `json:"draft"`
	Published *VoucherVersion `json:"published"`
	Previous  *VoucherVersion `json:"previous"`
}

func voucherVersion(db queryer, slot VoucherSlot) (*VoucherVersion, error) {
	v := &VoucherVersion{}
	err := db.QueryRow("SELECT version, saved_at, voucher_html, voucher_css, custom_js FROM voucher_versions WHERE slot = ?", slot).
		Scan(&v.Version, &v.SavedAt, &v.Files.VoucherHTML, &v.Files.VoucherCSS, &v.Files.CustomJS)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return v, err
}

// putVoucher stores v in slot, or empties the slot when v is nil.
func putVoucher(tx *sql.Tx, slot VoucherSlot, v *VoucherVersion) error {
	if v == nil {
		_, err := tx.Exec("DELETE FROM voucher_versions WHERE slot = ?", slot)
		return err
	}
	_, err := tx.Exec(`INSERT INTO voucher_versions (slot, version, voucher_html, voucher_css, custom_js, saved_at) VALUES (?, ?, ?, ?, ?, ?)
	 ON CONFLICT(slot) DO UPDATE SET version = excluded.version, voucher_html = excluded.voucher_html,
	 voucher_css = excluded.voucher_css, custom_js = excluded.custom_js,
	 saved_at = excluded.saved_at`,
		slot, v.Version, v.Files.VoucherHTML, v.Files.VoucherCSS, v.Files.CustomJS, v.SavedAt)
	return err
}

// VoucherVersion reads one slot; nil if it is empty.
func (l *Ledger) VoucherVersion(slot VoucherSlot) (*VoucherVersion, error) {
	return voucherVersion(l.db, slot)
}

func (l *Ledger) VoucherState() (VoucherState, error) {
	draft, err := voucherVersion(l.db, VoucherDraft)
	if err != nil {
		return VoucherState{}, err
	}
	published, err := voucherVersion(l.db, VoucherPublished)
	if err != nil {
		return VoucherState{}, err
	}
	previous, err := voucherVersion(l.db, VoucherPrevious)
	return VoucherState{draft, published, previous}, err
}

// SaveVoucherDraft stores checked files without changing the published template.
func (l *Ledger) SaveVoucherDraft(files VoucherFiles) (int64, error) {
	v := &VoucherVersion{SavedAt: time.Now().Unix(), Files: files}
	err := l.voucherChange(func(tx *sql.Tx, _ map[VoucherSlot]*VoucherVersion) error {
		if err := tx.QueryRow("SELECT COALESCE(MAX(version), 0) + 1 FROM voucher_versions").Scan(&v.Version); err != nil {
			return err
		}
		return putVoucher(tx, VoucherDraft, v)
	})
	return v.Version, err
}

// PublishVoucher makes the draft what Print sheets use and keeps the published
// version as previous. Republishing the published draft changes nothing.
func (l *Ledger) PublishVoucher() error {
	return l.voucherChange(func(tx *sql.Tx, slots map[VoucherSlot]*VoucherVersion) error {
		draft, published := slots[VoucherDraft], slots[VoucherPublished]
		if draft == nil {
			return ErrNoVoucherDraft
		}
		if published != nil && published.Version == draft.Version {
			return nil
		}
		if published == nil {
			published = &VoucherVersion{} // Remember the default without storing its files.
		}
		if err := putVoucher(tx, VoucherPrevious, published); err != nil {
			return err
		}
		return putVoucher(tx, VoucherPublished, draft)
	})
}

// RevertVoucher swaps the published and previous versions. After a Reset to
// default the previous version is the one Reset removed, so Revert undoes it.
func (l *Ledger) RevertVoucher() error {
	return l.voucherChange(func(tx *sql.Tx, slots map[VoucherSlot]*VoucherVersion) error {
		if slots[VoucherPrevious] == nil {
			return ErrNoVoucherPrevious
		}
		previous, published := slots[VoucherPrevious], slots[VoucherPublished]
		if previous.Version == 0 {
			previous = nil
		}
		if published == nil {
			published = &VoucherVersion{}
		}
		if err := putVoucher(tx, VoucherPublished, previous); err != nil {
			return err
		}
		return putVoucher(tx, VoucherPrevious, published)
	})
}

// ResetVoucher empties the published slot so Print sheets use the default Voucher template,
// keeping what was published as previous.
func (l *Ledger) ResetVoucher() error {
	return l.voucherChange(func(tx *sql.Tx, slots map[VoucherSlot]*VoucherVersion) error {
		if slots[VoucherPublished] == nil {
			return nil
		}
		if err := putVoucher(tx, VoucherPrevious, slots[VoucherPublished]); err != nil {
			return err
		}
		return putVoucher(tx, VoucherPublished, nil)
	})
}

// voucherChange runs change in one transaction, handing it every slot as read inside it.
func (l *Ledger) voucherChange(change func(*sql.Tx, map[VoucherSlot]*VoucherVersion) error) error {
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	slots := map[VoucherSlot]*VoucherVersion{}
	for _, slot := range []VoucherSlot{VoucherDraft, VoucherPublished, VoucherPrevious} {
		if slots[slot], err = voucherVersion(tx, slot); err != nil {
			return err
		}
	}
	if err := change(tx, slots); err != nil {
		return err
	}
	return tx.Commit()
}

// ShopName is independent of template versions, like the other Shop settings.
func (l *Ledger) ShopName() (string, error) {
	var name string
	err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'shop_name'").Scan(&name)
	return name, err
}

func (l *Ledger) SaveShopName(name string) error {
	_, err := l.db.Exec("UPDATE settings SET value = ? WHERE key = 'shop_name'", name)
	return err
}
