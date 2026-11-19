package ledger

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"golang.org/x/crypto/bcrypt"
	_ "modernc.org/sqlite"
)

type Ledger struct {
	db   *sql.DB
	path string
}

func Open(path string) (*Ledger, error) {
	path, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("resolve ledger path: %w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("create ledger: %w", err)
	}
	if err := file.Chmod(0o600); err != nil {
		file.Close()
		return nil, fmt.Errorf("protect ledger: %w", err)
	}
	if err := file.Close(); err != nil {
		return nil, fmt.Errorf("close ledger file: %w", err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open ledger: %w", err)
	}
	db.SetMaxOpenConns(1)

	for _, statement := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA synchronous=FULL",
		"PRAGMA foreign_keys=ON",
		`CREATE TABLE IF NOT EXISTS settings (
			key TEXT PRIMARY KEY,
			value BLOB NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS router_config (
			id INTEGER PRIMARY KEY CHECK (id = 1),
			address TEXT NOT NULL,
			username TEXT NOT NULL,
			api_secret TEXT NOT NULL,
			certificate BLOB NOT NULL,
			fingerprint TEXT NOT NULL
		)`,
		adminSessionSchema,
		voucherSchema,
		voucherDeletionSchema,
		rebindSchema,
		pauseSchema,
		creditIntentSchema,
		validitySchema,
		timeCreditProfileSchema,
		portalSchema,
		voucherTemplateSchema,
		portalArrivalSchema,
	} {
		if _, err := db.Exec(statement); err != nil {
			db.Close()
			return nil, fmt.Errorf("initialize ledger: %w", err)
		}
	}

	store := &Ledger{db: db, path: path}
	if err := store.initializeVendos(); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize Vendos: %w", err)
	}
	if err := store.initializeCoinWindows(); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize Coin Windows: %w", err)
	}
	if _, err := db.Exec(oversightSchema); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize oversight: %w", err)
	}
	if err := store.initializeSales(); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize Sales: %w", err)
	}
	if err := store.migrateRateTierLimits(); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate Rate tier limits: %w", err)
	}
	if err := store.migratePoints(); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate Points: %w", err)
	}
	if err := store.migrateVoucherBatches(); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate Voucher Batches: %w", err)
	}
	if err := store.initializeInstallKey(); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize portal identity: %w", err)
	}
	if err := store.backfillResumeCodes(); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize Resume Codes: %w", err)
	}
	if err := store.initializeAdoptedServers(); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialize Adopted HotSpot servers: %w", err)
	}
	return store, nil
}

func (l *Ledger) Close() error {
	return l.db.Close()
}

func (l *Ledger) InitializeAdmin(password string) error {
	var existing []byte
	err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'admin_password_hash'").Scan(&existing)
	if err == nil {
		return nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("read admin password: %w", err)
	}
	if password == "" {
		return errors.New("OPENPISO_ADMIN_PASSWORD must be set for the first run")
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return fmt.Errorf("hash admin password: %w", err)
	}
	if _, err := l.db.Exec("INSERT INTO settings (key, value) VALUES ('admin_password_hash', ?)", hash); err != nil {
		return fmt.Errorf("store admin password: %w", err)
	}
	return nil
}

func (l *Ledger) AuthenticateAdmin(password string) bool {
	var hash []byte
	if err := l.db.QueryRow("SELECT value FROM settings WHERE key = 'admin_password_hash'").Scan(&hash); err != nil {
		return false
	}
	return bcrypt.CompareHashAndPassword(hash, []byte(password)) == nil
}

type RouterConfig struct {
	Address     string
	Username    string
	APISecret   string
	Certificate []byte
	Fingerprint string
}

func (l *Ledger) SaveRouterConfig(config RouterConfig) error {
	_, err := l.db.Exec(`INSERT INTO router_config (id, address, username, api_secret, certificate, fingerprint)
		VALUES (1, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			address = excluded.address,
			username = excluded.username,
			api_secret = excluded.api_secret,
			certificate = excluded.certificate,
			fingerprint = excluded.fingerprint`,
		config.Address, config.Username, config.APISecret, config.Certificate, config.Fingerprint)
	if err != nil {
		return fmt.Errorf("store Router configuration: %w", err)
	}
	return nil
}

func (l *Ledger) RouterConfig() (RouterConfig, bool, error) {
	var config RouterConfig
	err := l.db.QueryRow(`SELECT address, username, api_secret, certificate, fingerprint
		FROM router_config WHERE id = 1`).Scan(
		&config.Address, &config.Username, &config.APISecret, &config.Certificate, &config.Fingerprint)
	if errors.Is(err, sql.ErrNoRows) {
		return RouterConfig{}, false, nil
	}
	if err != nil {
		return RouterConfig{}, false, fmt.Errorf("read Router configuration: %w", err)
	}
	return config, true, nil
}

type DatabaseState struct {
	JournalMode string `json:"journal_mode"`
	Synchronous string `json:"synchronous"`
}

func (l *Ledger) DatabaseState() (DatabaseState, error) {
	var state DatabaseState
	var synchronous int
	if err := l.db.QueryRow("PRAGMA journal_mode").Scan(&state.JournalMode); err != nil {
		return state, fmt.Errorf("read journal mode: %w", err)
	}
	if err := l.db.QueryRow("PRAGMA synchronous").Scan(&synchronous); err != nil {
		return state, fmt.Errorf("read synchronous mode: %w", err)
	}
	if synchronous == 2 {
		state.Synchronous = "full"
	} else {
		state.Synchronous = fmt.Sprintf("%d", synchronous)
	}
	return state, nil
}
