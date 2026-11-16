"""ARMv7 self-update E2E for run.py `--armv7 --only update`; needs no CHR lab.

Installs a v0.1.0 build stamped with a throwaway release key through the real install.sh,
with --release-url pointing at a release tree this process serves on the host, reached by
the guest over an SSH reverse tunnel (the host firewall drops other guest-to-host traffic).
The signing seed lives only in this process's memory and the releasetool child's environment.

Ways the updater can fail, each asserted below unless marked otherwise:

 1. Release host unreachable (offline box, wrong clock breaking TLS): the check records
    check_error, exits 0 so the timer's unit is not failed, and offers nothing.
 2. Manifest signed by a key other than the compiled-in one: the check refuses it and
    offers nothing.
 3. Published release not newer than the running one (stale mirror, replayed old manifest):
    nothing is offered, and the admin Update endpoint refuses with 409.
 4. Asset bytes differ from the signed SHA-256 (tampered or truncated download): apply
    refuses before stopping the Controller, which keeps the same process throughout, and
    leaves no staged file behind.
 5. A correctly signed binary that reports a different version than its manifest (a release
    mix-up): apply runs it as the openpiso user with -version and refuses before stopping.
 6. A correctly signed binary that cannot serve (here: it scribbles over the ledger, like a
    migration gone wrong, and exits): /healthz stays down for 120 s, so apply restores the
    previous binary and the database backup, the Controller is healthy again, the ledger
    is intact (admin login works) and status.json says rolled-back.
 7. The happy path: timer-path check offers v0.2.0, Update on the admin page installs it,
    /healthz returns, `openpiso -version` is v0.2.0, the ledger and the admin session
    survive, the previous binary is kept, and status.json says installed.
 8. A compromised Controller plants the request file as a symlink to a root-owned file:
    only the link is removed (as the openpiso user); the target is untouched.
 9. The request file left in place would retrigger the path unit until its start limit:
    apply removes it as the openpiso user (root holds no CAP_DAC_OVERRIDE for the 0700 data
    directory) and records a failure to remove as a failed attempt. After every press the
    file is gone and openpiso-update.service ran exactly once.
10. Reinstalling over an updated install keeps the units, the timer and path unit active,
    the state directory and status.json.
11. The rollback itself fails (backup unreadable, previous binary missing): recorded as
    failed with both reasons. Not asserted: it needs a broken root filesystem.
12. A check racing an apply: both take a lock in the state directory and the check skips.
    Not asserted: the timing cannot be forced from outside.
13. The helper dies mid-apply (power loss): the Controller ignores an applying marker older
    than 15 minutes so the Update button comes back. Not asserted: needs a power cut.
"""

import http.cookiejar
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
ASSET_KEYS = ("linux-armv7", "linux-arm64", "linux-amd64")
PLATFORMS = {"linux-armv7": {"GOARCH": "arm", "GOARM": "7"}, "linux-arm64": {"GOARCH": "arm64"}, "linux-amd64": {"GOARCH": "amd64"}}
GUEST_RELEASE_PORT = 18090  # the guest's end of the reverse tunnel
RELEASE_URL = f"http://127.0.0.1:{GUEST_RELEASE_PORT}"
STATUS = "/var/lib/openpiso-update/status.json"
REQUEST = "/var/lib/openpiso/update-request"
HTTP_TIMEOUT = 120
APPLY_TIMEOUT = 900  # download, restart, and for a rollback 120 s of failed health checks, under TCG

# A correctly signed release that cannot serve: like a migration gone wrong, it overwrites the
# ledger, then exits, so /healthz never answers. It answers -version like the real binary.
BROKEN_MAIN = """package main

import (
	"fmt"
	"os"
	"path/filepath"
)

var version = "dev"

func main() {
	data := "."
	for i, arg := range os.Args {
		if arg == "-version" {
			fmt.Println(version)
			return
		}
		if arg == "-data" && i+1 < len(os.Args) {
			data = os.Args[i+1]
		}
	}
	os.WriteFile(filepath.Join(data, "openpiso.db"), []byte("not a database"), 0o600)
	fmt.Fprintln(os.Stderr, "broken release: wrote over the ledger")
	os.Exit(1)
}
"""


def run_tool(*args, env=None, cwd=CONTROLLER):
    result = subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, text=True, capture_output=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result.stdout


class Admin:
    """An admin session on the guest Controller over the management network."""

    def __init__(self, base: str, password: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, _ = self.call("/admin/login", {"password": password})
        assert status == 200, f"admin login: HTTP {status}"

    def call(self, path: str, form=None):
        data = None if form is None else urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(self.base + path, data=data, method="GET" if form is None else "POST")
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def json(self, path: str):
        status, body = self.call(path)
        assert status == 200, f"GET {path}: HTTP {status} {body}"
        return json.loads(body)


class Releases:
    """Throwaway-key builds and the release tree the guest updates from.

    The tree has the GitHub Releases layout update.go reads: latest/download/release.json(.sig)
    and download/<version>/openpiso-<asset key>. It is served on host loopback only and reaches
    the guest's 127.0.0.1:18090 through an SSH reverse tunnel.
    """

    def __init__(self):
        go = os.environ.get("GO") or shutil.which("go")
        if go is None:
            raise RuntimeError("the update E2E needs the Go toolchain: put go on PATH or set GO")
        self.go = go
        self.directory = tempfile.TemporaryDirectory(prefix="openpiso-update-e2e-")
        self.work = Path(self.directory.name)
        self.tree = self.work / "tree"
        self.tree.mkdir()
        self.requests = []
        self.releasetool = self.work / "releasetool"
        run_tool(self.go, "build", "-o", str(self.releasetool), "./cmd/releasetool", env={"CGO_ENABLED": "0"})
        self.seed, self.public_key = self.keygen()
        self.rogue_seed, _ = self.keygen()  # a valid key the binaries do not trust
        print("Building throwaway-key releases v0.1.0, v0.2.0, v0.3.0 (cannot serve) and v0.4.0", flush=True)
        for version in ("v0.1.0", "v0.2.0", "v0.4.0"):
            run_tool("make", "-C", str(CONTROLLER), "cross", f"GO={self.go}", f"VERSION={version}",
                     f"RELEASE_KEY={self.public_key}", f"OUT={self.builds(version)}")
        self.build_broken("v0.3.0")
        self.server = None

    def keygen(self):
        # The seed stays in this process and the releasetool child's environment, never a file.
        output = dict(line.split("=", 1) for line in run_tool(str(self.releasetool), "keygen").split())
        return output["private"], output["public"]

    def builds(self, version: str) -> Path:
        return self.work / "builds" / version

    def binary(self, version: str) -> Path:
        return self.builds(version) / "openpiso-linux-armv7"

    def build_broken(self, version: str):
        source = self.work / "broken-source"
        source.mkdir()
        (source / "go.mod").write_text("module brokenrelease\n\ngo 1.22\n")
        (source / "main.go").write_text(BROKEN_MAIN)
        for key, platform in PLATFORMS.items():
            run_tool(self.go, "build", "-ldflags", f"-X main.version={version}", "-o", str(self.builds(version) / f"openpiso-{key}"), ".",
                     env={"CGO_ENABLED": "0", "GOOS": "linux", **platform}, cwd=source)

    def serve(self):
        requests = self.requests

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                requests.append(self.path)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: Handler(*args, directory=str(self.tree)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def publish(self, version: str, binaries: Path, seed: str, tamper: bool = False):
        """Signs binaries as `version` with seed and makes it the latest release. tamper flips one
        byte of the armv7 asset after signing, keeping its size."""
        release = self.tree / "download" / version
        release.mkdir(parents=True, exist_ok=True)
        for key in ASSET_KEYS:
            shutil.copyfile(binaries / f"openpiso-{key}", release / f"openpiso-{key}")
        run_tool(str(self.releasetool), "manifest", "-version", version, "-dir", str(release), env={"RELEASE_SIGNING_KEY": seed})
        if tamper:
            asset = release / "openpiso-linux-armv7"
            data = bytearray(asset.read_bytes())
            data[len(data) // 2] ^= 0xFF
            asset.write_bytes(data)
        latest = self.tree / "latest" / "download"
        latest.mkdir(parents=True, exist_ok=True)
        for name in ("release.json", "release.json.sig"):
            shutil.copyfile(release / name, latest / f"{name}.tmp")
            (latest / f"{name}.tmp").replace(latest / name)

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.directory.cleanup()

    # --- the scenario -------------------------------------------------------------------------

    def run(self, vm, report, password: str):
        report.data["test_seam"] = "install.sh and the update units in an ARMv7 guest; admin HTTP; release tree over an SSH reverse tunnel"
        vm.reverse_tunnel(GUEST_RELEASE_PORT, self.serve())
        vm.install(password, release_url=RELEASE_URL)
        vm.start_controller()
        base = f"http://{vm.address}:18080"
        wait_healthy(base)
        report.data["execution"] = vm.install_evidence()
        assert_units(vm)
        assert version(vm) == "v0.1.0"
        admin = Admin(base, password)
        dashboard = admin.json("/admin/dashboard?format=json")
        assert dashboard["controller"]["version"] == "v0.1.0", dashboard["controller"]
        env = vm.ssh("sudo grep '^OPENPISO_RELEASE_URL=' /etc/openpiso/openpiso.env")
        assert env == f"OPENPISO_RELEASE_URL={RELEASE_URL}", env
        step(report, "installed-v0.1.0-timer-and-path-unit-active", dashboard_version="v0.1.0", release_url_env=env)

        # 1. Nothing published: the release host answers 404, like an unreachable one.
        status = run_check(vm, lambda s: "check_error" in s)
        assert not status.get("available"), status
        step(report, "check-records-unreachable-release-and-exits-0", status=status)

        # 2. Signed by a key the binary does not trust.
        self.publish("v0.2.0", self.builds("v0.2.0"), self.rogue_seed)
        status = run_check(vm, lambda s: "signature is invalid" in s.get("check_error", ""))
        assert not status.get("available"), status
        step(report, "check-refuses-manifest-signed-by-another-key", status=status)

        # 7. The happy path, from the timer's check unit to the admin page's Update button.
        self.publish("v0.2.0", self.builds("v0.2.0"), self.seed)
        status = run_check(vm, lambda s: s.get("available") == "v0.2.0")
        assert "check_error" not in status, status
        assert admin.json("/admin/dashboard?format=json")["controller"]["update"]["available"] == "v0.2.0"
        code, page = admin.call("/admin/dashboard")
        assert code == 200 and "Update available: v0.2.0" in page, page
        status = press_update(vm, admin, status, "Updating to v0.2.0")
        attempt = status["last_apply"]
        assert (attempt["outcome"], attempt["from"], attempt["to"]) == ("installed", "v0.1.0", "v0.2.0"), status
        assert not status.get("available") and not status.get("applying"), status
        wait_healthy(base)
        assert version(vm) == "v0.2.0"
        assert vm.ssh("/usr/local/bin/openpiso.previous -version") == "v0.1.0"
        assert admin.json("/admin/update/state") == {"version": "v0.2.0", "pending": False}  # the session survived
        Admin(base, password)  # and the ledger's admin password
        assert_request_gone(vm)
        step(report, "update-button-installs-v0.2.0-and-keeps-ledger", status=status)

        # 3. The latest release is the running one: nothing offered, and the button refuses.
        status = run_check(vm, lambda s: "check_error" not in s and not s.get("available"))
        code, body = admin.call("/admin/update", {})
        assert code == 409 and "no update available" in body, (code, body)
        assert_request_gone(vm)
        step(report, "not-newer-release-not-offered-and-update-refused", status=status)

        # 6. A signed release that cannot serve and wrecks the ledger: roll back binary and ledger.
        self.publish("v0.3.0", self.builds("v0.3.0"), self.seed)
        status = run_check(vm, lambda s: s.get("available") == "v0.3.0")
        before = service_identity(vm)
        status = press_update(vm, admin, status, "Updating to v0.3.0")
        attempt = status["last_apply"]
        assert (attempt["outcome"], attempt["from"], attempt["to"]) == ("rolled-back", "v0.2.0", "v0.3.0"), status
        assert "did not become healthy" in attempt["reason"], status
        wait_healthy(base)
        assert version(vm) == "v0.2.0"
        assert service_identity(vm)["MainPID"] != before["MainPID"], "the rollback scenario never restarted the Controller"
        assert vm.ssh("sudo head -c 15 /var/lib/openpiso/openpiso.db") == "SQLite format 3"
        Admin(base, password)
        code, page = admin.call("/admin/dashboard")
        assert code == 200 and "was rolled back" in page, page
        assert_request_gone(vm)
        step(report, "unservable-release-rolled-back-with-ledger", status=status)

        # 4. Correctly signed manifest, tampered asset: refused before the Controller stops.
        self.publish("v0.4.0", self.builds("v0.4.0"), self.seed, tamper=True)
        status = run_check(vm, lambda s: s.get("available") == "v0.4.0")
        before = service_identity(vm)
        status = press_update(vm, admin, status, "Updating to v0.4.0")
        attempt = status["last_apply"]
        assert attempt["outcome"] == "failed" and "sha256 does not match" in attempt["reason"], status
        assert "/download/v0.4.0/openpiso-linux-armv7" in self.requests, "the tampered asset was never fetched"
        assert service_identity(vm) == before, "a refused update stopped the Controller"
        assert version(vm) == "v0.2.0"
        assert vm.ssh("find /usr/local/bin -maxdepth 1 -name '.openpiso-update-*'") == "", "a staged file was left behind"
        assert_request_gone(vm)
        step(report, "tampered-asset-refused-without-stopping", status=status, service=before)

        # 5. Correctly signed, but the binary is v0.2.0 published as v0.5.0.
        self.publish("v0.5.0", self.builds("v0.2.0"), self.seed)
        status = run_check(vm, lambda s: s.get("available") == "v0.5.0")
        status = press_update(vm, admin, status, "Updating to v0.5.0")
        attempt = status["last_apply"]
        assert attempt["outcome"] == "failed" and 'reports version "v0.2.0"' in attempt["reason"], status
        assert service_identity(vm) == before, "a refused update stopped the Controller"
        assert_request_gone(vm)
        step(report, "mislabelled-release-refused-without-stopping", status=status)

        # 8. The Controller plants the request as a symlink to a root-owned file.
        target = vm.ssh("sha256sum /etc/hostname; stat -c %F /etc/hostname")
        previous = status["last_apply"]
        runs = apply_runs(vm)
        vm.ssh(f"sudo -u openpiso ln -s /etc/hostname {REQUEST}")
        status = wait_one_apply(vm, previous, runs)
        assert_request_gone(vm)
        assert vm.ssh("sha256sum /etc/hostname; stat -c %F /etc/hostname") == target
        assert service_identity(vm) == before
        step(report, "symlinked-request-only-unlinked", status=status, target=target)

        # 10. Reinstall over the updated install.
        vm.ssh("cp /usr/local/bin/openpiso /tmp/openpiso-install/openpiso-reinstall")
        vm.ssh(vm.install_command("/tmp/openpiso-install/openpiso-reinstall", RELEASE_URL), input=password + "\n")
        wait_healthy(base)
        assert_units(vm)
        assert version(vm) == "v0.2.0"
        after = read_status(vm)
        assert after["last_apply"] == status["last_apply"], after
        Admin(base, password)
        step(report, "reinstall-keeps-units-and-update-state", status=after)


def step(report, name: str, **evidence):
    report.data["steps"].append({"name": name, "evidence": evidence})
    print(f"PASS {name}", flush=True)


def wait_healthy(base: str, timeout: float = 300):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=10) as response:
                if response.status == 204:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"{base}/healthz did not answer within {timeout:g} s")
        time.sleep(2)


def version(vm) -> str:
    return vm.ssh("/usr/local/bin/openpiso -version")


def assert_units(vm):
    units = "openpiso-update-check.timer openpiso-update.path"
    states = vm.ssh(f"systemctl is-enabled {units}; systemctl is-active {units}").split()
    assert states == ["enabled", "enabled", "active", "active"], states


def assert_request_gone(vm):
    assert vm.ssh(f"sudo test -e {REQUEST} || sudo test -L {REQUEST} || echo gone") == "gone", "update request left behind"


def read_status(vm) -> dict:
    return json.loads(vm.ssh(f"cat {STATUS}"))


def run_check(vm, expected) -> dict:
    """Starts the unit the daily timer starts and returns status.json once expected(status). A
    timer-fired check may already be running with the previous release, so try twice."""
    for _ in range(2):
        vm.ssh("sudo systemctl start openpiso-update-check.service")
        result = vm.ssh("systemctl show --property Result --value openpiso-update-check.service")
        assert result == "success", f"check unit result: {result}"
        status = read_status(vm)
        if expected(status):
            return status
    raise AssertionError(f"unexpected check result: {status}")


def service_identity(vm) -> dict:
    output = vm.ssh("systemctl show --property MainPID --property ExecMainStartTimestamp --property NRestarts openpiso")
    return dict(line.split("=", 1) for line in output.splitlines())


def apply_runs(vm) -> int:
    """How many times openpiso-update.service has started, from systemd's journal lines."""
    return int(vm.ssh("sudo journalctl --unit openpiso-update.service --output cat --no-pager | grep -c '^Starting ' || true"))


def press_update(vm, admin, status: dict, heading: str) -> dict:
    runs = apply_runs(vm)
    code, page = admin.call("/admin/update", {})
    assert code == 200 and heading in page, (code, page)
    return wait_one_apply(vm, status.get("last_apply"), runs)


def wait_one_apply(vm, previous, runs: int) -> dict:
    """Waits for the apply, then checks the request is gone and the path unit did not start the
    service again (a retrigger follows within moments of the run ending)."""
    status = wait_applied(vm, previous)
    time.sleep(10)
    assert_request_gone(vm)
    assert apply_runs(vm) == runs + 1, f"openpiso-update.service ran {apply_runs(vm) - runs} times for one request"
    return status


def wait_applied(vm, previous) -> dict:
    """Waits for an apply after `previous` (the last_apply before it) to finish."""
    deadline = time.monotonic() + APPLY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            status = read_status(vm)
        except (RuntimeError, ValueError):
            status = {}
        if status.get("last_apply") not in (None, previous) and not status.get("applying"):
            return status
        time.sleep(5)
    raise AssertionError(f"no apply outcome within {APPLY_TIMEOUT} s; last status: {status}")
