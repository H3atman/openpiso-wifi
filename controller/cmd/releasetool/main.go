// Command releasetool generates the release signing key, signs release manifests and verifies
// them; used by CI and the setup wizard.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/H3atman/openpiso-wifi/controller/internal/update"
)

func main() {
	if len(os.Args) < 2 {
		log.Fatal("usage: releasetool keygen | manifest -version vX.Y.Z -dir DIR | verify -dir DIR -pub FILE")
	}
	var err error
	switch os.Args[1] {
	case "keygen":
		err = keygen()
	case "manifest":
		err = manifest(os.Args[2:])
	case "verify":
		err = verify(os.Args[2:])
	default:
		err = fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		log.Fatal(err)
	}
}

func keygen() error {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	fmt.Printf("private=%s\npublic=%s\n", base64.StdEncoding.EncodeToString(priv.Seed()), base64.StdEncoding.EncodeToString(pub))
	return nil
}

// verify checks DIR/release.json.sig against DIR/release.json with the base64 public key in the
// -pub file, then checks every asset the manifest lists against the files in DIR. CI runs it
// before publishing, so a signing secret that does not match the committed key fails the release.
func verify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	dir := fs.String("dir", "", "directory holding release.json, release.json.sig and the binaries")
	pubFile := fs.String("pub", "", "file holding the base64 Ed25519 public key")
	fs.Parse(args)
	if *dir == "" || *pubFile == "" {
		return fmt.Errorf("usage: releasetool verify -dir DIR -pub FILE")
	}
	pubText, err := os.ReadFile(*pubFile)
	if err != nil {
		return err
	}
	pub, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(pubText)))
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return fmt.Errorf("%s is not a base64 Ed25519 public key", *pubFile)
	}
	body, err := os.ReadFile(filepath.Join(*dir, "release.json"))
	if err != nil {
		return err
	}
	sigText, err := os.ReadFile(filepath.Join(*dir, "release.json.sig"))
	if err != nil {
		return err
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigText)))
	if err != nil || !ed25519.Verify(ed25519.PublicKey(pub), body, sig) {
		return fmt.Errorf("release.json.sig does not verify against %s", *pubFile)
	}
	var m update.Manifest
	if err := json.Unmarshal(body, &m); err != nil {
		return fmt.Errorf("parse release.json: %w", err)
	}
	if _, err := update.ParseVersion(m.Version); err != nil {
		return err
	}
	for _, key := range update.AssetKeys {
		asset, ok := m.Assets[key]
		if !ok {
			return fmt.Errorf("release.json has no asset %s", key)
		}
		if err := checkAsset(filepath.Join(*dir, "openpiso-"+key), asset); err != nil {
			return err
		}
	}
	fmt.Printf("release %s verified: signature and %d assets match\n", m.Version, len(update.AssetKeys))
	return nil
}

// checkAsset compares the file at path with the size and SHA-256 the manifest lists.
func checkAsset(path string, want update.Asset) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	hash := sha256.New()
	n, err := io.Copy(hash, f)
	if err != nil {
		return err
	}
	if n != want.Size {
		return fmt.Errorf("%s: size is %d, manifest says %d", path, n, want.Size)
	}
	if got := hex.EncodeToString(hash.Sum(nil)); got != want.SHA256 {
		return fmt.Errorf("%s: sha256 is %s, manifest says %s", path, got, want.SHA256)
	}
	return nil
}

// manifest writes DIR/release.json and DIR/release.json.sig for the binaries in DIR.
func manifest(args []string) error {
	fs := flag.NewFlagSet("manifest", flag.ExitOnError)
	version := fs.String("version", "", "release version, vMAJOR.MINOR.PATCH")
	dir := fs.String("dir", "", "directory holding openpiso-<asset-key> binaries")
	fs.Parse(args)
	if _, err := update.ParseVersion(*version); err != nil {
		return err
	}
	seed, err := base64.StdEncoding.DecodeString(os.Getenv("RELEASE_SIGNING_KEY"))
	if err != nil || len(seed) != ed25519.SeedSize {
		return fmt.Errorf("RELEASE_SIGNING_KEY must be a base64 32-byte Ed25519 seed")
	}
	m := update.Manifest{Version: *version, Assets: map[string]update.Asset{}}
	for _, key := range update.AssetKeys {
		data, err := os.ReadFile(filepath.Join(*dir, "openpiso-"+key))
		if err != nil {
			return err
		}
		sum := sha256.Sum256(data)
		m.Assets[key] = update.Asset{SHA256: hex.EncodeToString(sum[:]), Size: int64(len(data))}
	}
	body, err := json.Marshal(m)
	if err != nil {
		return err
	}
	sig := ed25519.Sign(ed25519.NewKeyFromSeed(seed), body)
	if err := os.WriteFile(filepath.Join(*dir, "release.json"), body, 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(*dir, "release.json.sig"), []byte(base64.StdEncoding.EncodeToString(sig)+"\n"), 0o644)
}
