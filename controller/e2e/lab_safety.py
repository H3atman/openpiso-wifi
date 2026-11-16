"""Shared CHR lab guardrails. Refusals never change lab state."""

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess
import sys

LOCK = Path(f"/tmp/openpiso-chr-lab-{os.getuid()}.lock")


@contextmanager
def lab_lock(timeout: float):
    """Hold the CHR lab for one run; queue behind another holder for up to `timeout` seconds."""
    # Keep the same inode: unlinking a lock file can allow two simultaneous holders.
    with LOCK.open("a+") as lock:
        def flock(*wait: str) -> bool:
            return subprocess.run(["flock", *wait, str(lock.fileno())], pass_fds=(lock.fileno(),)).returncode == 0

        def holder() -> str:
            lock.seek(0)
            return lock.read().strip() or "an external flock process (no holder metadata)"

        if not flock("-n"):
            print(f"CHR lab is locked by {holder()}; waiting up to {timeout:g}s. Lock: {LOCK}", file=sys.stderr, flush=True)
            if not flock("-w", str(timeout)):
                sys.exit(f"CHR lab is locked by {holder()}; gave up after {timeout:g}s. Lock: {LOCK}")
        lock.seek(0)
        lock.truncate()
        lock.write(f"pid {os.getpid()} ({Path(sys.argv[0]).name})\n")
        lock.flush()
        yield


def preflight(root: Path) -> None:
    networks = subprocess.run(["virsh", "-c", "qemu:///system", "net-list", "--name"],
                              check=True, capture_output=True, text=True, timeout=10).stdout
    conflict = "openpiso-dev-routing network" if "openpiso-dev-routing" in networks.split() else ""
    if not conflict:
        nics = subprocess.run(["virsh", "-c", "qemu:///system", "domiflist", "chr-lab"],
                              check=True, capture_output=True, text=True, timeout=10).stdout
        if "openpiso-dev-routing" in nics or "52:54:00:07:08:07" in nics.lower():
            conflict = "openpiso-dev-routing routing NIC on chr-lab"
    if conflict:
        fix = shlex.join(["python3", "-I", str(root / "lab/dev/dev.py"), "down"])
        sys.exit(f"CHR lab preflight: conflicting {conflict}. Stop the dev lab, then run:\n{fix}\n"
                 "No lab state was changed. Do not remove shared state without its owner's approval.")
