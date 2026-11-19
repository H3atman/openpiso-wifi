"""Fresh stock armhf guest for run.py; no separate scenario list or fake Router. ArmContainer runs
it under QEMU (TCG) in a container on the Docker lab."""

import fcntl
import hashlib
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

IMAGE_BASE = "https://cloud-images.ubuntu.com/releases/bionic/release-20230607/"
# Literal hashes from that release's SHA256SUMS and unpacked/SHA256SUMS.
ARTIFACTS = {
    "ubuntu-18.04-server-cloudimg-armhf.img": "d3da9dfdd69857fd35cbfa562be84d013dfd024a72de55a8c6911268f76375c3",
    "unpacked/ubuntu-18.04-server-cloudimg-armhf-vmlinuz-lpae": "44678e6f87bc764eb29c022b8b59bb8cb42333232a7e2d3027053bc328d5aba8",
    "unpacked/ubuntu-18.04-server-cloudimg-armhf-initrd-generic-lpae": "4076869995177d12acb9dbae444c8e891857cf24e842a283d499fd2f0499957c",
}



def sha256_of(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def command(*args, **kwargs):
    try:
        return subprocess.run(args, check=True, text=True, capture_output=True, timeout=120, **kwargs).stdout.strip()
    except subprocess.CalledProcessError as error:
        # The default message omits the remote script's output, which is the diagnosis.
        raise RuntimeError(f"{args[0]} exited {error.returncode}\nstdout:\n{error.stdout}\nstderr:\n{error.stderr}") from None


def write_seed(seed: Path, instance: str, public_key: str) -> None:
    """The cloud-init meta-data and user-data both guests boot with: a new SSH key, no swap."""
    (seed / "meta-data").write_text(f"instance-id: {instance}\nlocal-hostname: openpiso-armv7\n")
    (seed / "user-data").write_text(
        "#cloud-config\n"
        "users:\n"
        "  - name: openpiso-e2e\n"
        "    shell: /bin/sh\n"
        "    lock_passwd: true\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    ssh_authorized_keys:\n"
        f"      - {public_key}\n"
        "ssh_pwauth: false\n"
        "disable_root: true\n"
        "package_update: false\n"
        "package_upgrade: false\n"
        "swap:\n  size: 0\n"
        "bootcmd:\n  - [swapoff, -a]\n"
        "runcmd:\n  - [touch, /run/openpiso-e2e-ready]\n"
    )


GUEST_ADDRESS = "10.77.7.1"  # the Controller's address on the Docker lab WAN
ADMIN_ADDRESS = "10.77.7.252"  # armv7.sh's helper address, where the host's SSH and HTTP arrive from
GUEST_MAC = "02:00:00:77:00:01"  # armv7.sh's guest NIC
# Static: the Router container's DHCP helper hands out 10.77.7.2 only. Devices are reached via the Router.
NETWORK_CONFIG = f"""version: 2
ethernets:
  lab:
    match: {{macaddress: "{GUEST_MAC}"}}
    set-name: eth0
    addresses: [{GUEST_ADDRESS}/24]
    routes: [{{to: 10.5.0.0/16, via: 10.77.7.2}}]
"""


def cached_artifacts(cache: Path) -> list[Path]:
    """The pinned image, kernel and initrd in cache, named by sha256 and shared by every worktree:
    downloaded on first use, verified on every use. A lock serializes first downloads from parallel
    runs; a file appears only once verified."""
    cache.mkdir(parents=True, exist_ok=True)
    paths = []
    with (cache / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name, digest in ARTIFACTS.items():
            path = cache / digest
            if not path.exists():
                print(f"Downloading pinned ARM artifact: {name}", flush=True)
                partial = cache / f"{digest}.{uuid.uuid4().hex}.part"
                try:
                    with urllib.request.urlopen(IMAGE_BASE + name, timeout=120) as response, partial.open("xb") as output:
                        shutil.copyfileobj(response, output)
                    if sha256_of(partial) != digest:
                        raise RuntimeError(f"ARM artifact checksum mismatch: {IMAGE_BASE + name}; nothing was cached")
                    partial.replace(path)
                finally:
                    partial.unlink(missing_ok=True)
            elif sha256_of(path) != digest:
                raise RuntimeError(f"ARM artifact checksum mismatch: {path} ({name}); not using it, delete it to download it again")
            paths.append(path)
    return paths


class ArmContainer:
    """The guest under QEMU (TCG) in a lab-image container (lab/docker/armv7.sh), at the
    Controller's lab address on a Docker network. lab is the loaded docker_lab module.

    boot(lab_name) joins that Docker lab's WAN in the Controller's place, and the lab removes the
    container with the rest of it; boot() makes a WAN network of its own (the boot and update checks),
    which close() removes with the container. The harness reaches the guest's SSH and Controller
    through the container's published ports; the serial console is the container's log."""

    def __init__(self, binary: Path, boot_timeout: float, serial_report: Path, lab):
        self.lab = lab
        self.binary = binary.resolve()
        self.boot_timeout = boot_timeout
        self.serial_report = serial_report.resolve()
        self.serial_log = None
        self.name = ""  # the container, once boot runs it
        self.directory = None
        self.address = "127.0.0.1"
        self.ssh_port = 22
        # Seconds, up to and including sshd's banner. On a busy host the TCG guest's sshd can take over
        # 5 s to send it (seen at load 18). A guest still booting fails at once instead: socat closes
        # the published port's connection.
        self.connect_timeout = 30
        self.controller_port = ""
        self.network = None  # the WAN network this guest made for itself
        self.logs = None
        self.ssh_options = []  # extra options for every ssh and scp
        self.created = False
        self.installed = False
        self.tunnels = []
        self.evidence = {
            "architecture": "linux/arm/v7",
            "vm_memory_mib": 256,
            "boot_timeout_seconds": boot_timeout,
            "image_url": IMAGE_BASE + next(iter(ARTIFACTS)),
            "image_sha256": next(iter(ARTIFACTS.values())),
            "boot_artifacts": {IMAGE_BASE + name: digest for name, digest in ARTIFACTS.items()},
            "container_memory_cap_mib": lab.ARM_MEMORY >> 20,
        }

    @property
    def base_url(self) -> str:
        """Where the harness reaches the guest's Controller."""
        return f"http://{self.controller_port}"

    def ssh(self, script: str, input=None):
        return command(
            "ssh", "-p", str(self.ssh_port), "-i", str(self.work / "id_ed25519"),
            "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.work / 'known_hosts'}", *self.ssh_options,
            f"openpiso-e2e@{self.address}", script, input=input,
        )

    def copy(self, source: Path, target: str):
        command(
            "scp", "-O", "-P", str(self.ssh_port), "-i", str(self.work / "id_ed25519"),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.work / 'known_hosts'}", *self.ssh_options,
            str(source), f"openpiso-e2e@{self.address}:{target}",
        )

    def serial_tail(self):
        if self.serial_log is None or not self.serial_log.exists():
            return "(serial log unavailable)"
        with self.serial_log.open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 16384))
            return log.read().decode("utf-8", errors="replace") or "(serial log empty)"

    def install(self, password: str, release_url: str | None = None):
        self.ssh("mkdir -m 700 /tmp/openpiso-install")
        for source in (self.binary, *sorted((Path(__file__).parents[1] / "install").iterdir())):
            if source.is_file():
                self.copy(source, f"/tmp/openpiso-install/{'openpiso' if source == self.binary else source.name}")
        self.ssh(self.install_command("/tmp/openpiso-install/openpiso", release_url), input=password + "\n")
        self.installed = True
        # Kernel VmHWM is a lifetime high-water mark, not an instantaneous sample.
        # Keep sampling across ALL service restarts, not only a short smoke run.
        sampler = self.work / "sample-rss.sh"
        sampler.write_text("""#!/bin/sh
while :; do
    pid=$(systemctl show --property MainPID --value openpiso)
    if [ "$pid" -gt 0 ] 2>/dev/null; then
        while [ -r "/proc/$pid/status" ]; do
            awk '/^VmHWM:/ {print $2}' "/proc/$pid/status" >> /run/openpiso-e2e-rss-kib
            sleep 0.1
        done
    fi
    sleep 0.1
done
""")
        self.copy(sampler, "/tmp/openpiso-install/sample-rss.sh")
        self.ssh("sudo systemd-run --unit=openpiso-e2e-rss /bin/sh /tmp/openpiso-install/sample-rss.sh")
        self.stop_controller()

    def start_controller(self):
        # The installer already initialized this fresh guest's one private ledger.
        # The scenario's restart-without-password exercises the installed unit as-is.
        self.ssh("sudo systemctl start openpiso")

    def record_peak(self):
        self.ssh("pid=$(systemctl show --property MainPID --value openpiso); "
                 "if [ \"$pid\" -gt 0 ] && [ -r /proc/$pid/status ]; then "
                 "awk '/^VmHWM:/ {print $2}' /proc/$pid/status | sudo tee -a /run/openpiso-e2e-rss-kib >/dev/null; fi")

    def kill_controller(self):
        # Crash the Controller; stop afterwards so Restart=on-failure cannot race the scenario.
        self.record_peak()
        self.ssh("sudo systemctl kill --signal=KILL openpiso; sudo systemctl stop openpiso")

    def reverse_tunnel(self, guest_port: int, host_port: int):
        """Forwards the guest's 127.0.0.1:guest_port to the host's 127.0.0.1:host_port over SSH;
        the host firewall drops other guest-to-host connections."""
        tunnel = subprocess.Popen(
            ["ssh", "-N", "-p", str(self.ssh_port), "-i", str(self.work / "id_ed25519"),
             "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.work / 'known_hosts'}",
             "-R", f"127.0.0.1:{guest_port}:127.0.0.1:{host_port}", f"openpiso-e2e@{self.address}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.tunnels.append(tunnel)
        deadline = time.monotonic() + 60
        while self.ssh(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{guest_port}/ || true") in ("", "000"):
            if tunnel.poll() is not None:
                raise RuntimeError(f"SSH reverse tunnel exited {tunnel.returncode}: {tunnel.stderr.read()}")
            if time.monotonic() > deadline:
                raise RuntimeError("SSH reverse tunnel did not come up within 60 seconds")
            time.sleep(1)

    def stop_controller(self):
        self.ssh("pid=$(systemctl show --property MainPID --value openpiso); "
                 "if [ \"$pid\" -gt 0 ] && [ -r /proc/$pid/status ]; then "
                 "awk '/^VmHWM:/ {print $2}' /proc/$pid/status | sudo tee -a /run/openpiso-e2e-rss-kib >/dev/null; fi; "
                 "sudo systemctl stop openpiso")
        result = self.ssh("systemctl show --property Result --value openpiso")
        if result != "success":
            raise AssertionError(f"ARM Controller service result: {result}")

    def install_evidence(self):
        output = self.ssh(
            "uname -m; awk '/^MemTotal:/ {print $2} /^SwapTotal:/ {print $2}' /proc/meminfo; "
            "systemctl show --property User --value openpiso; "
            "test -d /var/log/journal && "
            "grep -q '^Storage=persistent$' /etc/systemd/journald.conf.d/openpiso-journal.conf && "
            "grep -q '^SystemMaxUse=32M$' /etc/systemd/journald.conf.d/openpiso-journal.conf && "
            "grep -q '^SyncIntervalSec=1m$' /etc/systemd/journald.conf.d/openpiso-journal.conf && "
            "grep -q '^ForwardToSyslog=no$' /etc/systemd/journald.conf.d/openpiso-journal.conf && "
            "sudo journalctl --disk-usage >/dev/null && "
            "test -n \"$(sudo journalctl --directory=/var/log/journal --unit=openpiso --lines=1 --output=cat --quiet)\" && echo persistent-capped"
        ).splitlines()
        self.evidence.update({
            "machine": output[0], "memory_total_kib": int(output[1]), "swap_total_kib": int(output[2]),
            "service_user": output[3], "logs_persistent_capped": output[4:] == ["persistent-capped"],
            "binary_size_bytes": self.binary.stat().st_size,
        })
        return self.evidence

    def boot_evidence(self):
        output = self.ssh("uname -m; awk '/^MemTotal:/ {print $2} /^SwapTotal:/ {print $2}' /proc/meminfo").splitlines()
        self.evidence.update({
            "machine": output[0],
            "memory_total_kib": int(output[1]),
            "swap_total_kib": int(output[2]),
            "serial_console_tail": self.serial_tail(),
        })
        self.retain_serial(self.evidence)
        return dict(self.evidence)

    def peak_rss_kib(self):
        if not self.installed:
            return 0
        return int(self.ssh("sudo awk 'BEGIN {peak=0} $1>peak {peak=$1} END {print peak}' /run/openpiso-e2e-rss-kib"))

    def retain_serial(self, evidence):
        if self.serial_log is None or not self.serial_log.exists():
            return
        self.serial_report.parent.mkdir(parents=True, exist_ok=True)
        with self.serial_log.open("rb") as source, self.serial_report.open("wb") as output:
            shutil.copyfileobj(source, output)
        self.serial_report.chmod(0o600)
        evidence["serial_console_log"] = str(self.serial_report)
        print(f"ARM serial log retained: {self.serial_report}", flush=True)

    def failure_evidence(self):
        try:
            self.container_memory()
        except AssertionError:
            pass  # the container is gone already
        evidence = dict(self.evidence)
        evidence["serial_console_tail"] = self.serial_tail()
        self.retain_serial(evidence)
        try:
            evidence["peak_controller_rss_kib"] = self.peak_rss_kib()
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            evidence["peak_controller_rss_kib"] = None
        return evidence

    def boot(self, lab_name: str | None = None):
        started = time.monotonic()
        for tool in ("docker", "ssh", "scp", "ssh-keygen"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"ARM E2E requires {tool}; install host prerequisites first")
        guest, kernel, initrd = cached_artifacts(self.lab.CACHE / "armv7")
        image = self.lab.lab_image()
        self.directory = tempfile.TemporaryDirectory(prefix="openpiso-armv7-")
        self.work = Path(self.directory.name)
        command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.work / "id_ed25519"))
        seed = self.work / "seed"
        seed.mkdir(mode=0o700)
        write_seed(seed, f"openpiso-armv7-{uuid.uuid4().hex[:12]}", (self.work / "id_ed25519.pub").read_text().strip())
        (seed / "network-config").write_text(NETWORK_CONFIG)
        if lab_name is None:
            lab_name = "opnwlab-" + secrets.token_hex(3)
            self.network = f"{lab_name}-wan"
            self.lab.sh("docker", "network", "create", self.network)
        self.name = f"{lab_name}-armv7"
        cap = f"{self.lab.ARM_MEMORY >> 20}m"
        self.lab.sh("docker", "run", "-d", "--name", self.name, "--cap-add", "NET_ADMIN", "--device", "/dev/net/tun",
                    "--memory", cap, "--memory-swap", cap, "--network", f"{lab_name}-wan",
                    "-p", "127.0.0.1::2222", "-p", "127.0.0.1::18080",
                    "-v", f"{guest}:/img/guest.img:ro", "-v", f"{kernel}:/img/vmlinuz:ro", "-v", f"{initrd}:/img/initrd:ro",
                    "-v", f"{seed}:/seed:ro", "-v", f"{self.lab.LAB_DIR / 'armv7.sh'}:/lab/armv7.sh:ro",
                    image, "/bin/sh", "/lab/armv7.sh")
        self.created = True
        self.serial_log = self.work / "serial.log"
        with self.serial_log.open("wb") as log:
            self.logs = subprocess.Popen(["docker", "logs", "-f", self.name], stdout=log, stderr=subprocess.STDOUT)
        self.ssh_port = int(self.lab.sh("docker", "port", self.name, "2222").split()[0].rpartition(":")[2])
        self.controller_port = self.lab.sh("docker", "port", self.name, "18080").split()[0]
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            state = self.lab.sh("docker", "inspect", "-f", "{{.State.Status}} oom={{.State.OOMKilled}}", self.name).strip()
            if not state.startswith("running"):
                raise RuntimeError(f"ARM guest container {self.name} stopped ({state})\nSerial console tail:\n{self.serial_tail()}")
            try:
                self.ssh("test -f /run/openpiso-e2e-ready")
            except (RuntimeError, subprocess.SubprocessError):
                time.sleep(2)
                continue
            self.evidence["boot_seconds"] = round(time.monotonic() - started, 3)
            self.evidence["guest_address"] = GUEST_ADDRESS
            self.share_connection()
            return
        raise RuntimeError(
            f"ARM guest did not finish boot/cloud-init within {self.boot_timeout:g} seconds\n"
            f"Serial console tail:\n{self.serial_tail()}"
        )

    def share_connection(self):
        """Runs every later ssh and scp over one master connection. Under TCG each new connection's
        handshake costs the guest seconds, and the Router's meter runs on wall time: without this a
        Controller restart outlasted the scenario's 120-second Time Credit. A command after the master
        is gone connects on its own (ssh falls back when the socket does not answer)."""
        control = self.work / "control"
        master = subprocess.Popen(
            ["ssh", "-M", "-N", "-p", str(self.ssh_port), "-i", str(self.work / "id_ed25519"),
             "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}", "-o", f"ControlPath={control}",
             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.work / 'known_hosts'}",
             f"openpiso-e2e@{self.address}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.tunnels.append(master)
        deadline = time.monotonic() + 60
        while not control.exists():
            if master.poll() is not None:
                raise RuntimeError(f"SSH master connection exited {master.returncode}: {master.stderr.read()}")
            if time.monotonic() > deadline:
                raise RuntimeError("SSH master connection did not come up within 60 seconds")
            time.sleep(0.2)
        self.ssh_options = ["-o", f"ControlPath={control}"]

    def install_command(self, binary: str, release_url: str | None = None) -> str:
        return ("sudo sh /tmp/openpiso-install/install.sh "
                f"--binary {binary} --portal-url http://{GUEST_ADDRESS}:18080 --listen 0.0.0.0:18080 "
                f"--admin-network {ADMIN_ADDRESS}/32 --password-stdin"
                + (f" --release-url {release_url}" if release_url else ""))

    def container_memory(self) -> dict:
        """QEMU's resident high-water mark (guest RAM, translation cache, QEMU itself) and the cgroup's OOM
        kills, against the container's cap; kept in the evidence. Not the cgroup's own peak: page cache
        fills that up to any cap."""
        output = self.lab.sh("docker", "exec", self.name, "sh", "-c",
                             "awk '/^VmHWM:/ {print $2}' /proc/1/status; awk '$1 == \"oom_kill\" {print $2}' /sys/fs/cgroup/memory.events")
        peak_kib, oom_kills = output.split()
        self.evidence["qemu_peak_rss_mib"] = round(int(peak_kib) / 1024, 1)
        self.evidence["container_oom_kills"] = int(oom_kills)
        return {key: self.evidence[key] for key in ("container_memory_cap_mib", "qemu_peak_rss_mib", "container_oom_kills")}

    def close(self):
        for tunnel in self.tunnels:
            tunnel.terminate()
            tunnel.wait(timeout=10)
        self.tunnels = []
        if self.network is not None:
            if self.created:
                self.lab.sh("docker", "rm", "-f", "-v", self.name)
            self.lab.sh("docker", "network", "rm", self.network)
            self.network = None
        self.created = False
        if self.logs is not None:
            self.logs.terminate()  # ends by itself once the container is gone; not when --keep leaves it
            self.logs.wait(timeout=10)
        if self.directory is not None:
            self.directory.cleanup()
