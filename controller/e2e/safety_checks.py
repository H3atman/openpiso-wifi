#!/usr/bin/env python3
"""No-CHR command checks: python3 -I controller/e2e/safety_checks.py [case]."""

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(f"/tmp/openpiso-chr-lab-{os.getuid()}.lock")


class SafetyChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openpiso-safety-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        self.stub("virsh", 'echo "UNEXPECTED LAB ACCESS" >&2; exit 90')
        self.stub("ip", 'echo "UNEXPECTED LAB ACCESS" >&2; exit 90')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "$*" >> "{self.calls}"\n{body}\n')
        path.chmod(0o755)

    def entry(self, command=("make", "-C", str(ROOT / "controller"), "e2e")):
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, "refusal must fail")
        return result.stdout + result.stderr

    def test_held_lock(self):
        self.stub("go", 'echo "UNEXPECTED BUILD" >&2; exit 91')
        with LOCK.open("a+") as lock:
            # Never disturb an existing holder or its metadata.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.seek(0)
            lock.truncate()
            lock.write(f"pid {os.getpid()} (safety_checks.py)\n")
            lock.flush()
            # Same entry as `make e2e`, with a short queue so the wait gives up.
            output = self.entry((sys.executable, "-I", str(ROOT / "controller/e2e/run.py"), "--build",
                                 "--binary", str(ROOT / "controller/bin/openpiso"), "--lock-timeout", "1"))
            self.assertIn(f"CHR lab is locked by pid {os.getpid()} (safety_checks.py); waiting up to 1s", output)
            self.assertIn(f"CHR lab is locked by pid {os.getpid()} (safety_checks.py); gave up after 1s", output)
            self.assertFalse(self.calls.exists(), "held lock must precede all lab access")

    def test_conflicting_network(self):
        self.stub("virsh", '''case "$*" in
  "-c qemu:///system net-list --name") echo openpiso-dev-routing ;;
  *) echo "UNEXPECTED LAB ACCESS" >&2; exit 90 ;;
esac''')
        output = self.entry()
        self.assertIn("openpiso-dev-routing", output)
        self.assertIn(f"python3 -I {ROOT}/lab/dev/dev.py down", output)
        self.assertEqual(self.calls.read_text().splitlines(), ["-c qemu:///system net-list --name"])
        # A failed preflight must release the lock without deleting its inode.
        with LOCK.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_leftover_routing_nic(self):
        self.stub("virsh", '''case "$*" in
  "-c qemu:///system net-list --name") ;;
  "-c qemu:///system domiflist chr-lab") echo 'vnet9 network openpiso-dev-routing virtio 52:54:00:07:08:07' ;;
  *) echo "UNEXPECTED LAB ACCESS" >&2; exit 90 ;;
esac''')
        output = self.entry()
        self.assertIn("routing NIC", output)
        self.assertIn(f"python3 -I {ROOT}/lab/dev/dev.py down", output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "-c qemu:///system net-list --name", "-c qemu:///system domiflist chr-lab"])

    def test_worktree_credentials(self):
        main = self.root / "main checkout"
        main.mkdir()
        for relative in ("controller/e2e/run.py", "controller/e2e/simulated_vendo.py",
                         "controller/e2e/flash_pairing.py", "controller/e2e/lab_safety.py", "controller/e2e/browser_portal.py",
                         "controller/e2e/libvirt_lab.py", "lab/chr/vlan_lab.py", "lab/chr/e2e_lab.py"):
            target = main / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        git = ["git", "-C", str(main)]
        subprocess.run([*git, "init", "--quiet"], check=True, capture_output=True)
        subprocess.run([*git, "add", "."], check=True, capture_output=True)
        subprocess.run([*git, "-c", "user.name=Safety check", "-c", "user.email=safety@example.invalid",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "scratch fixture"],
                       check=True, capture_output=True)
        checkout = self.root / "linked checkout"
        subprocess.run([*git, "worktree", "add", "--quiet", "--detach", str(checkout)], check=True, capture_output=True)
        # Synthetic lab credentials only; never read or copy the real credentials in this check.
        (main / "lab/chr/.env").write_text("CHR_PASSWORD='synthetic-main-fixture'\n")
        self.stub("virsh", '''case "$*" in
  "-c qemu:///system net-list --name") echo openpiso-dev-routing ;;
  *) echo "UNEXPECTED LAB ACCESS" >&2; exit 90 ;;
esac''')
        result = subprocess.run([sys.executable, "-I", str(checkout / "controller/e2e/run.py"),
                                 "--binary", str(checkout / "controller/bin/openpiso")],
                                env=self.env, capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, "preflight must refuse")
        self.assertIn(f"Loaded lab credentials from {main}/lab/chr/.env", output)
        self.assertIn("CHR lab preflight: conflicting openpiso-dev-routing network", output)
        self.assertNotIn("synthetic-main-fixture", output)
        self.assertFalse((checkout / "lab/chr/.env").exists())
        self.assertFalse((checkout / "lab/chr/.env").is_symlink())
        self.assertEqual(self.calls.read_text().splitlines(), ["-c qemu:///system net-list --name"])


if __name__ == "__main__":
    unittest.main()
