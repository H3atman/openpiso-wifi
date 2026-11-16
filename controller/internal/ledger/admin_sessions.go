package ledger

import (
	"crypto/sha256"
	"fmt"
	"time"
)

const adminSessionSchema = `CREATE TABLE IF NOT EXISTS admin_sessions (
 token_hash BLOB PRIMARY KEY,
 expires_at INTEGER NOT NULL
)`

// CreateAdminSession stores only the token's hash and prunes expired sessions on login.
func (l *Ledger) CreateAdminSession(token string, now, expires time.Time) error {
	hash := sha256.Sum256([]byte(token))
	tx, err := l.db.Begin()
	if err != nil {
		return fmt.Errorf("begin admin session: %w", err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DELETE FROM admin_sessions WHERE expires_at <= ?", now.Unix()); err != nil {
		return fmt.Errorf("prune admin sessions: %w", err)
	}
	if _, err := tx.Exec("INSERT INTO admin_sessions (token_hash, expires_at) VALUES (?, ?)", hash[:], expires.Unix()); err != nil {
		return fmt.Errorf("store admin session: %w", err)
	}
	return tx.Commit()
}

// AdminSessionValid is read-only: the fixed expiry is never renewed by polling.
func (l *Ledger) AdminSessionValid(token string, now time.Time) (bool, error) {
	hash := sha256.Sum256([]byte(token))
	var valid bool
	err := l.db.QueryRow("SELECT EXISTS(SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires_at > ?)", hash[:], now.Unix()).Scan(&valid)
	if err != nil {
		return false, fmt.Errorf("read admin session: %w", err)
	}
	return valid, nil
}

func (l *Ledger) DeleteAdminSession(token string) error {
	hash := sha256.Sum256([]byte(token))
	if _, err := l.db.Exec("DELETE FROM admin_sessions WHERE token_hash = ?", hash[:]); err != nil {
		return fmt.Errorf("delete admin session: %w", err)
	}
	return nil
}
