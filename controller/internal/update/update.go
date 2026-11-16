// Package update implements the Controller's signed self-update. Trust comes from an Ed25519
// signature over release.json, checked against ReleaseKey compiled into the binary; the download
// host is not trusted. The package fetches, verifies and swaps files; the root helper in
// cmd/openpiso owns the service restart around it.
package update

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// ReleaseKey is the base64 raw Ed25519 public key that signs releases. It is set at build time
// from controller/release.pub; when empty every update operation fails.
var ReleaseKey string

// DefaultBaseURL is the GitHub Releases root of the project.
const DefaultBaseURL = "https://github.com/H3atman/openpiso-wifi/releases"

const (
	maxManifestBytes = 64 << 10
	maxBinaryBytes   = 64 << 20
)

// ErrNotNewer is returned by Stage when the published version is not strictly greater than the
// running one.
var ErrNotNewer = errors.New("no newer release")

var client = &http.Client{Timeout: 5 * time.Minute}

// Asset describes one release binary.
type Asset struct {
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Manifest is the signed release.json.
type Manifest struct {
	Version string           `json:"version"`
	Assets  map[string]Asset `json:"assets"`
}

// AssetKeys are the platforms a release must carry; the binary for each is named openpiso-<key>.
var AssetKeys = []string{"linux-armv7", "linux-arm64", "linux-amd64"}

// ParseVersion parses a strict vMAJOR.MINOR.PATCH version.
func ParseVersion(v string) ([3]uint64, error) {
	var out [3]uint64
	s, ok := strings.CutPrefix(v, "v")
	parts := strings.Split(s, ".")
	if !ok || len(parts) != 3 {
		return out, fmt.Errorf("version %q is not vMAJOR.MINOR.PATCH", v)
	}
	for i, p := range parts {
		n, err := strconv.ParseUint(p, 10, 32)
		if err != nil || (len(p) > 1 && p[0] == '0') {
			return out, fmt.Errorf("version %q is not vMAJOR.MINOR.PATCH", v)
		}
		out[i] = n
	}
	return out, nil
}

// Newer reports whether candidate is a strictly greater version than current; false when either
// does not parse (a "dev" build is never offered an update).
func Newer(candidate, current string) bool {
	c, err := ParseVersion(candidate)
	if err != nil {
		return false
	}
	have, err := ParseVersion(current)
	if err != nil {
		return false
	}
	for i := range c {
		if c[i] != have[i] {
			return c[i] > have[i]
		}
	}
	return false
}

// assetKey names the running platform in the manifest.
func assetKey() (string, error) {
	if runtime.GOOS == "linux" {
		switch runtime.GOARCH {
		case "arm":
			return "linux-armv7", nil
		case "arm64", "amd64":
			return "linux-" + runtime.GOARCH, nil
		}
	}
	return "", fmt.Errorf("no release asset for %s/%s", runtime.GOOS, runtime.GOARCH)
}

func publicKey() (ed25519.PublicKey, error) {
	if ReleaseKey == "" {
		return nil, errors.New("this build has no release key; self-update is disabled")
	}
	raw, err := base64.StdEncoding.DecodeString(ReleaseKey)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("compiled-in release key is not a base64 Ed25519 public key")
	}
	return ed25519.PublicKey(raw), nil
}

func get(url string) (*http.Response, error) {
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("GET %s: %s", url, resp.Status)
	}
	return resp, nil
}

func fetchSmall(url string) ([]byte, error) {
	resp, err := get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxManifestBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", url, err)
	}
	if len(body) > maxManifestBytes {
		return nil, fmt.Errorf("%s exceeds %d bytes", url, maxManifestBytes)
	}
	return body, nil
}

// Check fetches and verifies the latest manifest. current is the running version; isNewer
// reports whether the manifest's version is strictly greater (false when current does not parse).
func Check(baseURL, current string) (m Manifest, isNewer bool, err error) {
	key, err := publicKey()
	if err != nil {
		return m, false, err
	}
	baseURL = strings.TrimRight(baseURL, "/")
	body, err := fetchSmall(baseURL + "/latest/download/release.json")
	if err != nil {
		return m, false, err
	}
	sigText, err := fetchSmall(baseURL + "/latest/download/release.json.sig")
	if err != nil {
		return m, false, err
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigText)))
	if err != nil || !ed25519.Verify(key, body, sig) {
		return m, false, errors.New("release manifest signature is invalid")
	}
	if err := json.Unmarshal(body, &m); err != nil {
		return m, false, fmt.Errorf("parse release manifest: %w", err)
	}
	if _, err := ParseVersion(m.Version); err != nil {
		return m, false, err
	}
	return m, Newer(m.Version, current), nil
}

// Staged is a verified release binary waiting next to its target; Path is removed by Install
// (renamed into place) or should be removed by the caller when the update is abandoned.
type Staged struct {
	Version string
	Path    string
}

// Stage fetches the latest release when it is newer than current and downloads its binary for
// this platform into a hidden 0755 file in dir, verified against the signed size and SHA-256.
// Nothing else is touched, so a refusal here leaves the running Controller alone.
func Stage(baseURL, current, dir string) (Staged, error) {
	m, isNewer, err := Check(baseURL, current)
	if err != nil {
		return Staged{}, err
	}
	if !isNewer {
		return Staged{}, fmt.Errorf("%w: running %s, published %s", ErrNotNewer, current, m.Version)
	}
	key, err := assetKey()
	if err != nil {
		return Staged{}, err
	}
	asset, ok := m.Assets[key]
	if !ok {
		return Staged{}, fmt.Errorf("release %s has no asset %s", m.Version, key)
	}
	if asset.Size <= 0 || asset.Size > maxBinaryBytes {
		return Staged{}, fmt.Errorf("asset %s size %d is outside 1..%d", key, asset.Size, maxBinaryBytes)
	}
	want, err := hex.DecodeString(asset.SHA256)
	if err != nil || len(want) != sha256.Size {
		return Staged{}, fmt.Errorf("asset %s has an invalid sha256", key)
	}

	tmp, err := os.CreateTemp(dir, ".openpiso-update-*")
	if err != nil {
		return Staged{}, err
	}
	err = download(tmp, strings.TrimRight(baseURL, "/")+"/download/"+m.Version+"/openpiso-"+key, asset.Size, want)
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Chmod(tmp.Name(), 0o755)
	}
	if err != nil {
		os.Remove(tmp.Name())
		return Staged{}, err
	}
	return Staged{Version: m.Version, Path: tmp.Name()}, nil
}

// download streams url into dst, requiring exactly size bytes with the given SHA-256.
func download(dst *os.File, url string, size int64, want []byte) error {
	resp, err := get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(dst, hash), io.LimitReader(resp.Body, size+1))
	if err != nil {
		return fmt.Errorf("download %s: %w", url, err)
	}
	if n != size {
		return fmt.Errorf("download %s: got %d bytes, manifest says %d", url, n, size)
	}
	if hex.EncodeToString(hash.Sum(nil)) != hex.EncodeToString(want) {
		return fmt.Errorf("download %s: sha256 does not match manifest", url)
	}
	return dst.Sync()
}

// Install renames the staged binary over target, keeping the replaced one as target+".previous".
func Install(staged Staged, target string) error {
	if err := keepPrevious(target); err != nil {
		return fmt.Errorf("keep previous binary: %w", err)
	}
	return os.Rename(staged.Path, target)
}

// Restore puts target+".previous" back over target, undoing Install.
func Restore(target string) error {
	return os.Rename(target+".previous", target)
}

// keepPrevious preserves the current binary as path+".previous", by hard link when possible.
func keepPrevious(path string) error {
	prev := path + ".previous"
	if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err := os.Remove(prev); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Link(path, prev); err == nil {
		return nil
	}
	src, err := os.Open(path)
	if err != nil {
		return err
	}
	defer src.Close()
	dst, err := os.OpenFile(prev, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		dst.Close()
		os.Remove(prev)
		return err
	}
	return dst.Close()
}

// The root helper's state, read by the unprivileged Controller. The directory is root-owned so
// the Controller can never plant files or links in it.
const (
	StateDir   = "/var/lib/openpiso-update"
	StatusPath = StateDir + "/status.json"
	// RequestName is the empty file the Controller creates in its data directory to ask for an
	// update. Its existence is the whole message: root reads nothing from it.
	RequestName = "update-request"
)

// Outcomes of an apply attempt.
const (
	Installed  = "installed"
	RolledBack = "rolled-back"
	Failed     = "failed"
)

// Status is status.json: what the admin page shows about updates.
type Status struct {
	CheckedAt  time.Time `json:"checked_at,omitzero"`
	Available  string    `json:"available,omitempty"` // a published version newer than the running one
	CheckError string    `json:"check_error,omitempty"`
	Applying   *Attempt  `json:"applying,omitempty"` // set while an apply runs
	LastApply  *Attempt  `json:"last_apply,omitempty"`
}

// Attempt is one apply: From and At are set when it starts, To once a release is staged, and
// Outcome (Installed, RolledBack or Failed) with a short Reason when it ends.
type Attempt struct {
	From    string    `json:"from"`
	To      string    `json:"to,omitempty"`
	At      time.Time `json:"at"`
	Outcome string    `json:"outcome,omitempty"`
	Reason  string    `json:"reason,omitempty"`
}

// ReadStatus reads status.json; a missing file is os.ErrNotExist (no root helper installed).
func ReadStatus(path string) (Status, error) {
	var status Status
	body, err := os.ReadFile(path)
	if err != nil {
		return status, err
	}
	return status, json.Unmarshal(body, &status)
}

// WriteStatus replaces status.json atomically (temp file and rename) with mode 0644.
func WriteStatus(path string, status Status) error {
	body, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".status-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name()) // no-op after the rename
	_, err = tmp.Write(append(body, '\n'))
	if err == nil {
		err = tmp.Chmod(0o644)
	}
	if err == nil {
		err = tmp.Sync()
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}
