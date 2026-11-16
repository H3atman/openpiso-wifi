package main

import (
	"cmp"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/update"
)

// version is stamped at build time with -ldflags "-X main.version=...".
var version = "dev"

// The root helper. `openpiso update check` and `openpiso update apply` run as root from the
// openpiso-update-check and openpiso-update units; the Controller itself stays unprivileged and
// offline. The data directory belongs to the openpiso user (0700), so root never touches a path
// in it: the units hold no CAP_DAC_OVERRIDE, and apply re-executes this binary as openpiso
// (`update remove-request`, `update backup-ledger`, `update restore-ledger`) for those files.
const (
	dataDir       = "/var/lib/openpiso"
	serviceName   = "openpiso"
	serviceUser   = "openpiso"
	backupName    = "update-backup" // inside dataDir, written only as serviceUser
	healthTimeout = 120 * time.Second
)

// ledgerFiles are the SQLite database and its WAL files, backed up and restored together.
var ledgerFiles = []string{"openpiso.db", "openpiso.db-wal", "openpiso.db-shm"}

// runUpdate handles `openpiso update check|apply`.
func runUpdate(args []string) error {
	usage := errors.New("usage: openpiso update check|apply [-release-url URL] [-target PATH]")
	if len(args) == 0 {
		return usage
	}
	switch args[0] {
	case "remove-request": // run by apply as serviceUser
		if err := os.Remove(filepath.Join(dataDir, update.RequestName)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		return nil
	case "backup-ledger":
		return backupLedger()
	case "restore-ledger":
		return restoreLedger()
	case "check", "apply":
	default:
		return usage
	}
	fs := flag.NewFlagSet("update "+args[0], flag.ContinueOnError)
	releaseURL := fs.String("release-url", cmp.Or(os.Getenv("OPENPISO_RELEASE_URL"), update.DefaultBaseURL),
		"release base URL (default $OPENPISO_RELEASE_URL, else GitHub Releases)")
	target := fs.String("target", "/usr/local/bin/openpiso", "binary to replace (apply only)")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if args[0] == "check" {
		return check(*releaseURL)
	}
	return apply(*releaseURL, *target)
}

// check records the latest release in status.json. Being offline is normal for these boxes, so a
// failed check is recorded and reported on stderr but exits 0.
func check(baseURL string) error {
	unlock, locked, err := lockState(false)
	if err != nil {
		return err
	}
	if !locked {
		fmt.Fprintln(os.Stderr, "an update is being applied; skipping this check")
		return nil
	}
	defer unlock()
	status := readStatus()
	m, isNewer, checkErr := update.Check(baseURL, version)
	status.CheckedAt = time.Now().UTC()
	status.CheckError = ""
	if checkErr != nil {
		status.CheckError = checkErr.Error() // keep any Available found by an earlier check
	} else {
		status.Available = ""
		if isNewer {
			status.Available = m.Version
		}
	}
	if err := update.WriteStatus(update.StatusPath, status); err != nil {
		return err
	}
	if checkErr != nil {
		fmt.Fprintln(os.Stderr, "update check failed:", checkErr)
		return nil
	}
	fmt.Printf("current=%s available=%s update=%t\n", version, m.Version, isNewer)
	return nil
}

// apply installs the latest release, records the outcome in status.json, and returns an error
// unless it was installed.
func apply(baseURL, target string) error {
	unlock, _, err := lockState(true)
	if err != nil {
		return err
	}
	defer unlock()
	status := readStatus()
	attempt := &update.Attempt{From: version, At: time.Now().UTC()}
	status.Applying = attempt
	err = update.WriteStatus(update.StatusPath, status)
	// Remove the request only after the applying marker is written, so the admin page always sees
	// one or the other. A request left in place would retrigger the path unit after every run.
	account, removeErr := lookupAccount(serviceUser)
	if removeErr == nil {
		_, removeErr = account.output("/proc/self/exe", "update", "remove-request")
	}
	if removeErr != nil {
		removeErr = fmt.Errorf("remove the update request: %w", removeErr)
	}
	if err != nil {
		return errors.Join(err, removeErr)
	}

	var installErr error
	if removeErr != nil {
		attempt.Outcome, attempt.Reason = update.Failed, removeErr.Error()
		installErr = removeErr
	} else {
		installErr = installRelease(baseURL, target, account, attempt)
	}
	status.Applying, status.LastApply = nil, attempt
	if attempt.Outcome == update.Installed {
		status.Available = ""
	}
	if err := update.WriteStatus(update.StatusPath, status); err != nil {
		return errors.Join(installErr, err)
	}
	if installErr != nil {
		return installErr
	}
	fmt.Printf("updated %s: %s -> %s (previous kept as %s.previous)\n", target, attempt.From, attempt.To, target)
	return nil
}

// installRelease stages and verifies the release before stopping anything, then swaps binary and
// ledger backup in under a stopped Controller, rolling both back when it does not come up healthy.
// It fills in attempt's To, Outcome and Reason.
func installRelease(baseURL, target string, account account, attempt *update.Attempt) error {
	fail := func(err error) error {
		attempt.Outcome, attempt.Reason = update.Failed, err.Error()
		return err
	}
	listen := os.Getenv("OPENPISO_LISTEN")
	if listen == "" {
		return fail(errors.New("OPENPISO_LISTEN is not set"))
	}
	staged, err := update.Stage(baseURL, version, filepath.Dir(target))
	if err != nil {
		return fail(err)
	}
	defer os.Remove(staged.Path) // no-op once installed
	attempt.To = staged.Version
	out, err := account.output(staged.Path, "-version")
	if err != nil {
		return fail(fmt.Errorf("run %s -version: %w", staged.Version, err))
	}
	if reported := strings.TrimSpace(out); reported != staged.Version {
		return fail(fmt.Errorf("release %s binary reports version %q", staged.Version, reported))
	}

	// From here the Controller is down; every failure path starts it again.
	if err := systemctl("stop", serviceName); err != nil {
		return fail(errors.Join(err, startHealthy(listen)))
	}
	if _, err := account.output("/proc/self/exe", "update", "backup-ledger"); err != nil {
		return fail(errors.Join(fmt.Errorf("back up the ledger: %w", err), startHealthy(listen)))
	}
	if err := update.Install(staged, target); err != nil {
		return fail(errors.Join(err, startHealthy(listen)))
	}
	healthErr := startHealthy(listen)
	if healthErr == nil {
		attempt.Outcome = update.Installed
		return nil
	}

	// The ledger's migrations are forward-only, so the database goes back with the binary.
	reason := fmt.Sprintf("%s did not become healthy: %v", staged.Version, healthErr)
	err = errors.Join(
		systemctl("stop", serviceName),
		update.Restore(target),
		func() error {
			_, err := account.output("/proc/self/exe", "update", "restore-ledger")
			return err
		}(),
		startHealthy(listen),
	)
	if err != nil {
		return fail(fmt.Errorf("%s; rollback failed: %w", reason, err))
	}
	attempt.Outcome, attempt.Reason = update.RolledBack, reason
	return errors.New(reason)
}

// startHealthy starts the Controller and waits for its /healthz on the listen address, mapping
// an unspecified host (0.0.0.0, [::] or none) to loopback.
func startHealthy(listen string) error {
	systemctl("reset-failed", serviceName) // a crash-looping unit may have hit its start limit
	if err := systemctl("start", serviceName); err != nil {
		return err
	}
	host, port, err := net.SplitHostPort(listen)
	if err != nil {
		return fmt.Errorf("OPENPISO_LISTEN: %w", err)
	}
	if address, err := netip.ParseAddr(host); host == "" || (err == nil && address.IsUnspecified()) {
		host = "127.0.0.1"
		if err == nil && address.Is6() {
			host = "::1"
		}
	}
	url := "http://" + net.JoinHostPort(host, port) + "/healthz"
	probe := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(healthTimeout)
	for {
		response, err := probe.Get(url)
		if err == nil {
			io.Copy(io.Discard, io.LimitReader(response.Body, 1<<10))
			response.Body.Close()
			if response.StatusCode/100 == 2 {
				return nil
			}
			err = fmt.Errorf("GET /healthz: %s", response.Status)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("no healthy /healthz within %s (last: %w)", healthTimeout, err)
		}
		time.Sleep(time.Second)
	}
}

func systemctl(args ...string) error {
	out, err := exec.Command("systemctl", args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("systemctl %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

// account is the unprivileged user that owns the data directory.
type account struct{ uid, gid uint32 }

func lookupAccount(name string) (account, error) {
	u, err := user.Lookup(name)
	if err != nil {
		return account{}, err
	}
	uid, err := strconv.ParseUint(u.Uid, 10, 32)
	if err != nil {
		return account{}, err
	}
	gid, err := strconv.ParseUint(u.Gid, 10, 32)
	if err != nil {
		return account{}, err
	}
	return account{uint32(uid), uint32(gid)}, nil
}

// output runs a program as the account with no supplementary groups, an empty environment and /
// as working directory, and returns its stdout; stderr is passed through to the journal.
func (a account) output(program string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: a.uid, Gid: a.gid, Groups: []uint32{}}}
	cmd.Env = []string{}
	cmd.Dir = "/"
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	return string(out), err
}

// lockState takes the state directory lock that serializes check and apply. With wait false it
// returns locked=false instead of blocking.
func lockState(wait bool) (unlock func(), locked bool, err error) {
	file, err := os.OpenFile(filepath.Join(update.StateDir, "lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, false, err
	}
	how := syscall.LOCK_EX
	if !wait {
		how |= syscall.LOCK_NB
	}
	if err := syscall.Flock(int(file.Fd()), how); err != nil {
		file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, false, nil
		}
		return nil, false, err
	}
	return func() { file.Close() }, true, nil
}

// readStatus returns the recorded status, or an empty one when it is missing or unreadable.
func readStatus() update.Status {
	status, err := update.ReadStatus(update.StatusPath)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		fmt.Fprintln(os.Stderr, "replacing unreadable status:", err)
	}
	return status
}

// backupLedger copies the stopped Controller's database files into dataDir/update-backup. It
// runs as serviceUser.
func backupLedger() error {
	backup := filepath.Join(dataDir, backupName)
	if err := os.RemoveAll(backup); err != nil {
		return err
	}
	if err := os.Mkdir(backup, 0o700); err != nil {
		return err
	}
	for i, name := range ledgerFiles {
		err := copyFile(filepath.Join(dataDir, name), filepath.Join(backup, name))
		if errors.Is(err, os.ErrNotExist) && i > 0 { // the WAL files exist only after an unclean stop
			continue
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// restoreLedger puts the backup taken by backupLedger back, removing WAL files it did not have.
// It runs as serviceUser.
func restoreLedger() error {
	backup := filepath.Join(dataDir, backupName)
	if _, err := os.Stat(filepath.Join(backup, ledgerFiles[0])); err != nil {
		return err
	}
	for _, name := range ledgerFiles {
		live := filepath.Join(dataDir, name)
		tmp := filepath.Join(dataDir, "."+name+".restore")
		os.Remove(tmp)
		err := copyFile(filepath.Join(backup, name), tmp)
		if errors.Is(err, os.ErrNotExist) {
			if err := os.Remove(live); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
			continue
		}
		if err == nil {
			err = os.Rename(tmp, live)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func copyFile(source, destination string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	_, err = io.Copy(out, in)
	if err == nil {
		err = out.Sync()
	}
	if closeErr := out.Close(); err == nil {
		err = closeErr
	}
	return err
}
