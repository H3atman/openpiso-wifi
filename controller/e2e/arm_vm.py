"""Fresh stock armhf guest for run.py; no separate scenario list or fake Router."""

import hashlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

IMAGE_BASE = "https://cloud-images.ubuntu.com/releases/bionic/release-20230607/"
# Literal hashes from that release's SHA256SUMS and unpacked/SHA256SUMS.
ARTIFACTS = {
    "ubuntu-18.04-server-cloudimg-armhf.img": "d3da9dfdd69857fd35cbfa562be84d013dfd024a72de55a8c6911268f76375c3",
    "unpacked/ubuntu-18.04-server-cloudimg-armhf-vmlinuz-lpae": "44678e6f87bc764eb29c022b8b59bb8cb42333232a7e2d3027053bc328d5aba8",
    "unpacked/ubuntu-18.04-server-cloudimg-armhf-initrd-generic-lpae": "4076869995177d12acb9dbae444c8e891857cf24e842a283d499fd2f0499957c",
}


# Gitignored; files are named by their pinned sha256.
ARTIFACT_CACHE = Path(__file__).resolve().parent / ".cache"


def sha256_of(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def command(*args, **kwargs):
    try:
        return subprocess.run(args, check=True, text=True, capture_output=True, timeout=120, **kwargs).stdout.strip()
    except subprocess.CalledProcessError as error:
        # The default message omits the remote script's output, which is the diagnosis.
        raise RuntimeError(f"{args[0]} exited {error.returncode}\nstdout:\n{error.stdout}\nstderr:\n{error.stderr}") from None


class ArmVM:
    def __init__(self, binary: Path, boot_timeout: float, serial_report: Path):
        self.binary = binary.resolve()
        self.boot_timeout = boot_timeout
        self.serial_report = serial_report.resolve()
        self.serial_log = None
        self.name = f"openpiso-armv7-{uuid.uuid4().hex[:12]}"
        self.directory = None
        self.address = ""
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
        }

    def virsh(self, *args):
        return command("virsh", "-c", "qemu:///system", *args)

    def ssh(self, script: str, input=None):
        return command(
            "ssh", "-i", str(self.work / "id_ed25519"),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.work / 'known_hosts'}",
            f"openpiso-e2e@{self.address}", script, input=input,
        )

    def copy(self, source: Path, target: str):
        command(
            "scp", "-O", "-i", str(self.work / "id_ed25519"),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.work / 'known_hosts'}",
            str(source), f"openpiso-e2e@{self.address}:{target}",
        )

    def boot(self):
        started = time.monotonic()
        for tool in ("qemu-system-arm", "qemu-img", "virsh", "ssh", "scp", "ssh-keygen"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"ARM E2E requires {tool}; install host prerequisites first")
        iso_tool = next((path for tool in ("xorrisofs", "genisoimage", "mkisofs")
                         if (path := shutil.which(tool)) is not None), None)
        if iso_tool is None:
            raise RuntimeError("ARM E2E requires xorrisofs, genisoimage, or mkisofs; install host prerequisites first")
        # Never reuse a guest disk or mix downloads with our scripts/seed inputs.
        self.directory = tempfile.TemporaryDirectory(prefix="openpiso-armv7-", dir="/var/tmp")
        self.work = Path(self.directory.name)
        self.work.chmod(0o755)  # libvirt's unprivileged QEMU must traverse this directory.
        downloads = self.work / "downloads"
        downloads.mkdir(mode=0o755)
        artifacts = []
        for index, (name, digest) in enumerate(ARTIFACTS.items()):
            artifact_dir = downloads / str(index)
            artifact_dir.mkdir(mode=0o755)
            destination = artifact_dir / Path(name).name
            artifacts.append(destination)
            # Cached by sha256 and re-verified on every use; each run still gets its own copy
            # because libvirt's QEMU cannot traverse the repo and the disk backs onto the image.
            cached = ARTIFACT_CACHE / digest
            if not cached.is_file() or sha256_of(cached) != digest:
                print(f"Downloading pinned ARM artifact: {name}", flush=True)
                ARTIFACT_CACHE.mkdir(parents=True, exist_ok=True)
                partial = ARTIFACT_CACHE / f"{digest}.{uuid.uuid4().hex}.part"
                try:
                    with urllib.request.urlopen(IMAGE_BASE + name, timeout=120) as response, partial.open("xb") as output:
                        shutil.copyfileobj(response, output)
                    if sha256_of(partial) != digest:
                        raise RuntimeError(f"ARM artifact checksum mismatch: {name}")
                    partial.replace(cached)
                finally:
                    partial.unlink(missing_ok=True)
            else:
                print(f"Using cached pinned ARM artifact: {name}", flush=True)
            shutil.copyfile(cached, destination)
            if sha256_of(destination) != digest:
                raise RuntimeError(f"ARM artifact checksum mismatch after copy: {name}")
            destination.chmod(0o644)
        image, kernel, initrd = artifacts
        disk = self.work / "guest.qcow2"
        command("qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(image), str(disk), "4G")
        disk.chmod(0o600)  # libvirt DAC relabels this disk for QEMU, not world-writable.
        command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.work / "id_ed25519"))
        public_key = (self.work / "id_ed25519.pub").read_text().strip()
        seed = self.work / "seed-input"
        seed.mkdir(mode=0o700)
        (seed / "meta-data").write_text(f"instance-id: {self.name}\nlocal-hostname: openpiso-armv7\n")
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
        seed_image = self.work / "seed.iso"
        command(iso_tool, "-quiet", "-output", str(seed_image), "-volid", "cidata", "-joliet", "-rock",
                str(seed / "user-data"), str(seed / "meta-data"))
        seed_image.chmod(0o644)
        self.serial_log = self.work / "serial.log"
        self.serial_log.touch(mode=0o600)
        # libvirt-owned log files are root-only, so stream the console over loopback
        # TCP into a file this process owns.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            serial_port = probe.getsockname()[1]
        xml = self.work / "domain.xml"
        xml.write_text(f"""<domain type='qemu'>
  <name>{self.name}</name>
  <memory unit='MiB'>256</memory><currentMemory unit='MiB'>256</currentMemory>
  <vcpu>1</vcpu>
  <os><type arch='armv7l' machine='virt'>hvm</type>
    <kernel>{escape(str(kernel))}</kernel><initrd>{escape(str(initrd))}</initrd>
    <cmdline>console=ttyAMA0 root=LABEL=cloudimg-rootfs rw</cmdline>
  </os>
  <cpu mode='custom' match='exact'><model fallback='forbid'>cortex-a15</model></cpu>
  <devices>
    <emulator>{escape(shutil.which('qemu-system-arm'))}</emulator>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/>
      <source file='{escape(str(disk))}'/><target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='disk'><driver name='qemu' type='raw'/>
      <source file='{escape(str(seed_image))}'/><target dev='vdb' bus='virtio'/><readonly/>
    </disk>
    <interface type='network'><source network='default'/><model type='virtio'/></interface>
    <serial type='tcp'><source mode='bind' host='127.0.0.1' service='{serial_port}'/><protocol type='raw'/><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
    <memballoon model='none'/>
  </devices>
</domain>
""")
        self.virsh("create", str(xml))
        self.created = True
        threading.Thread(target=self.capture_serial, args=(serial_port,), daemon=True).start()
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            addresses = self.virsh("domifaddr", self.name, "--source", "lease")
            for line in addresses.splitlines():
                fields = line.split()
                if len(fields) >= 4 and fields[2] == "ipv4":
                    self.address = fields[3].split("/")[0]
            if self.address:
                try:
                    self.ssh("test -f /run/openpiso-e2e-ready")
                    self.evidence["boot_seconds"] = round(time.monotonic() - started, 3)
                    self.evidence["guest_address"] = self.address
                    return
                except (RuntimeError, subprocess.SubprocessError):
                    pass
            time.sleep(2)
        raise RuntimeError(
            f"ARM guest did not finish boot/cloud-init within {self.boot_timeout:g} seconds\n"
            f"Serial console tail:\n{self.serial_tail()}"
        )

    def capture_serial(self, port):
        deadline = time.monotonic() + 30
        while True:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                if time.monotonic() > deadline:
                    return
                time.sleep(0.2)
        connection.settimeout(None)
        with connection, self.serial_log.open("ab", buffering=0) as log:
            while chunk := connection.recv(4096):
                log.write(chunk)

    def serial_tail(self):
        if self.serial_log is None or not self.serial_log.exists():
            return "(serial log unavailable)"
        with self.serial_log.open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 16384))
            return log.read().decode("utf-8", errors="replace") or "(serial log empty)"

    def install_command(self, binary: str, release_url: str | None = None) -> str:
        return ("sudo sh /tmp/openpiso-install/install.sh "
                f"--binary {binary} --portal-url http://{self.address}:18080 --listen 0.0.0.0:18080 "
                "--admin-network 192.168.122.1/32 --password-stdin"
                + (f" --release-url {release_url}" if release_url else ""))

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
            ["ssh", "-N", "-i", str(self.work / "id_ed25519"),
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

    def route_customer_network(self, router_address: str):
        # The Router preserves Device source IPs to the guest; replies must return through it.
        self.ssh(f"sudo ip route replace 10.5.50.0/24 via {router_address}")

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
        evidence = dict(self.evidence)
        evidence["serial_console_tail"] = self.serial_tail()
        self.retain_serial(evidence)
        try:
            evidence["peak_controller_rss_kib"] = self.peak_rss_kib()
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            evidence["peak_controller_rss_kib"] = None
        return evidence

    def close(self):
        for tunnel in self.tunnels:
            tunnel.terminate()
            tunnel.wait(timeout=10)
        self.tunnels = []
        if self.created:
            # Only the uniquely named transient guest created by this run.
            self.virsh("destroy", self.name)
            self.created = False
        if self.directory is not None:
            self.directory.cleanup()
