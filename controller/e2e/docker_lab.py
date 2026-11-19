"""The Docker lab behind run.py's lab interface (issues #26, #28, #29): `run.py --lab docker`.

Each run gets its own throwaway, sealed lab: a stock RouterOS CHR under QEMU, the Controller binary,
an internet stand-in on the lab WAN, and Linux Device containers on the lab LAN. `running` brings it
up, takes the Router from stock to what onboarding needs, and removes it however the run ends (--keep
leaves it up). The pinned CHR image is downloaded, verified and cached on first use.

Device 1 has MAC 0C:01 and Device 2 0C:02, each its own container. A MAC change is a real one on
Device 1's interface: Device 2 leaves and Device 1 takes its MAC, as the scenarios expect.
A Vendo board is a container too, with its own MAC, lease and opnw-XXXX DHCP hostname (#30). It sends
its first contact and every later Report itself, from its own address and MAC.
The lab's own scenarios (sealed internet, memory caps, VLAN HotSpot login, IPv6) run in own_scenarios.
"""

import contextlib
import email.message
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = ROOT / "lab/docker"
CHR_VERSION = "7.23.8"  # the pinned RouterOS version; bump with CHR_SHA256
CHR_SHA256 = "bf8dc6cff65105389f29618f86c0d19e58a621465cb4754f0818a6ea50714ce7"  # of the .img inside CHR_URL
CHR_URL = f"https://download.mikrotik.com/routeros/{CHR_VERSION}/chr-{CHR_VERSION}.img.zip"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "openpiso"
ROUTER_PASSWORD = "docker-lab-router-admin"  # set by the harness on the stock Router's empty admin
ROUTER_WAN = "10.77.7.2"
PORTAL_URL = "http://10.77.7.1:18080"
UPSTREAM_URL = "http://1.1.1.1/"  # served by the internet stand-in
INTERNET_BODY = "openpiso-lab-internet"  # what the stand-in serves on every address
GOOGLE_ADDRESS = "142.250.4.100"  # what the stand-in resolves google.com to
REAL_INTERNET = "9.9.9.9"  # a public address the stand-in does not answer
ROUTER_MEMORY = 320 << 20
OTHER_MEMORY = 128 << 20
DEVICE_MACS = {1: "02:00:00:00:0C:01", 2: "02:00:00:00:0C:02"}  # Device 1 is lab/chr/e2e_lab.py's DEVICE_MAC
OWN_DEVICE_MAC = "02:00:00:00:0C:31"
VLAN_DEVICE_MAC = "02:00:00:00:0C:11"
VLAN_DEVICE_MACS = {10: "02:00:00:00:0C:10", 20: "02:00:00:00:0C:20"}  # vlan_up's Devices
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def sh(*command: str, check: bool = True, env=None) -> str:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(command)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
    return result.stdout


def verify_router_image(path: Path, sha256: str, source: str, hint: str) -> None:
    with path.open("rb") as file:
        actual = hashlib.file_digest(file, "sha256").hexdigest()
    if actual != sha256:
        raise SystemExit(f"Checksum mismatch for the RouterOS CHR {CHR_VERSION} image {source}: sha256 {actual}, "
                         f"expected {sha256} (CHR_SHA256 in controller/e2e/docker_lab.py). {hint}")


def router_image(sha256: str = CHR_SHA256) -> Path:
    """The pinned CHR image, downloaded from MikroTik on first use and cached for every worktree. Verified
    on every run. A lock serializes first downloads from parallel runs; the image appears only once verified."""
    cache = CACHE / "chr"
    cache.mkdir(parents=True, exist_ok=True)
    image = cache / f"chr-{CHR_VERSION}.img"
    with (cache / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not image.exists():
            print(f"Downloading {CHR_URL}", flush=True)
            with tempfile.TemporaryDirectory(dir=cache) as tmp:
                archive, extracted = Path(tmp) / "chr.zip", Path(tmp) / image.name
                with urllib.request.urlopen(CHR_URL, timeout=60) as response, archive.open("wb") as out:
                    shutil.copyfileobj(response, out)
                with zipfile.ZipFile(archive) as bundle, bundle.open(image.name) as src, extracted.open("wb") as out:
                    shutil.copyfileobj(src, out)
                verify_router_image(extracted, sha256, f"downloaded from {CHR_URL}", "Nothing was cached.")
                os.replace(extracted, image)
    verify_router_image(image, sha256, f"cached at {image}", "Not using it; delete it to download it again.")
    return image


def lab_image() -> str:
    """The one lab image every role runs, tagged by its Dockerfile's content: built once, shared by
    parallel and later runs, never removed by a run."""
    tag = "opnwlab-image:" + hashlib.sha256((LAB_DIR / "Dockerfile").read_bytes()).hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        sh("docker", "build", "-q", "-t", tag, str(LAB_DIR))
    return tag


class Lab:
    """One lab: its compose project, networks and Device containers all carry the `name` prefix."""

    def __init__(self, binary: Path, chr_image: Path, image: str):
        self.name = "opnwlab-" + secrets.token_hex(3)
        self.image = image
        self.env = {"LAB": self.name, "LAB_IMAGE": image, "CHR_IMAGE": str(chr_image), "CONTROLLER_BINARY": str(binary)}

    def compose(self, *args: str) -> str:
        return sh("docker", "compose", "-f", str(LAB_DIR / "compose.yml"), *args, env={**os.environ, **self.env})

    def up(self) -> None:
        self.compose("up", "-d")

    def port(self, service: str, port: int) -> str:
        return self.compose("port", service, str(port)).strip()

    def device(self, suffix: str, mac: str, ipv6: bool = False) -> str:
        """Start a Device on the LAN with its own MAC; it drops Docker's IPv4 address before any lease."""
        name = f"{self.name}-{suffix}"
        network = f"name={self.name}-lan"
        if ipv6:
            network += ",driver-opt=com.docker.network.endpoint.sysctls=net.ipv6.conf.IFNAME.disable_ipv6=0"
        sh("docker", "run", "-d", "--name", name, "--cap-add", "NET_ADMIN", "--network", network,
           "--mac-address", mac, "--memory", "128m", "--memory-swap", "128m", self.image, "sleep", "infinity")
        sh("docker", "exec", name, "sh", "-c", "ip -4 addr flush dev eth0; ip -4 route flush dev eth0")
        return name

    def containers(self) -> list[str]:
        return [name for name in sh("docker", "ps", "-a", "--format", "{{.Names}}").split()
                if name.startswith(self.name + "-")]

    def networks(self) -> list[str]:
        return [name for name in sh("docker", "network", "ls", "--format", "{{.Name}}").split()
                if name.startswith(self.name + "-")]

    def logs(self) -> dict:
        return {service: self.compose("logs", "--no-color", "--tail", "40", service)[-4000:]
                for service in ("router", "controller", "internet")}

    def remove_command(self) -> str:
        return (f"docker rm -f -v {' '.join(self.containers())} && "
                f"docker network rm {' '.join(self.networks())}")

    def down(self) -> None:
        """Remove every container and network carrying the lab's name, whatever point the run reached.
        Retries briefly in case Docker was still finishing a create the interrupted run had asked for."""
        for _ in range(3):
            if containers := self.containers():
                sh("docker", "rm", "-f", "-v", *containers, check=False)
            if networks := self.networks():
                sh("docker", "network", "rm", *networks, check=False)
            if not self.containers() and not self.networks():
                return
            time.sleep(1)
        raise AssertionError(f"lab {self.name} left behind: {self.containers() + self.networks()}")


def device_exec(device: str, script: str, check: bool = True) -> str:
    return sh("docker", "exec", device, "sh", "-c", script, check=check)


def device_lease(device: str, interface: str = "eth0", hostname: str = "") -> str:
    """Take a DHCP lease from the Router, sending hostname (DHCP option 12) when given, and return the
    Device's address. udhcpc's script cannot `mv` its resolv.conf over Docker's bind-mounted one and
    leaves it as resolv.conf.<pid>; copy that in place so the Device resolves through the DNS server its
    lease named."""
    option = f" -x hostname:{hostname}" if hostname else ""
    device_exec(device, f"udhcpc -i {interface}{option} -q -n -t 20 -T 2 && for f in /etc/resolv.conf.*; do "
                        "cat $f > /etc/resolv.conf && rm $f; done")
    output = device_exec(device, f"ip -o -4 addr show dev {interface}")
    return re.search(r"inet (\S+)/", output).group(1)


def device_http(device: str, url: str, form=None, cookie: str = "", follow: bool = False,
                max_time: int = 10) -> tuple[int, list[str], str]:
    """An HTTP request as the Device: status, the final reply's "Name: value" header lines, full body.
    follow follows redirects, as RouterOS /tool/fetch does."""
    command = ["docker", "exec", device, "curl", "-sS", "-i", "--max-time", str(max_time)]
    if follow:
        command.append("-L")
    if form is not None:
        command += ["--data", urllib.parse.urlencode(form)]
    if cookie:
        command += ["-H", "Cookie: " + cookie]
    rest = sh(*command, url)  # text mode has already turned CRLF into LF
    while rest.startswith("HTTP/"):  # one header block per redirect followed; the last one answers
        head, _, rest = rest.partition("\n\n")
    lines = head.split("\n")
    return int(lines[0].split()[1]), lines[1:], rest


def header(lines: list[str], name: str) -> str:
    return "\n".join(line.partition(":")[2].strip() for line in lines if line.lower().startswith(name.lower() + ":"))


def device_pings(device: str, address: str) -> bool:
    return " 0% packet loss" in device_exec(device, f"ping -c 3 -W 2 {address}", check=False)


def device_resolves(device: str, name: str) -> list[str]:
    """Addresses the Device's resolver (the DNS server its lease named) returns for `name`."""
    output = device_exec(device, f"nslookup -type=a {name}", check=False)
    return re.findall(r"Address: (\d+\.\d+\.\d+\.\d+)", output.partition("Name:")[2])


def redirected_before_time_credit(device: str, hotspot_address: str) -> dict:
    """The Device has no Time Credit: upstream is redirected to the Router's login, the Portal is walled in."""
    status, lines, body = device_http(device, UPSTREAM_URL)
    location = header(lines, "location")
    assert status == 302 and location.startswith(f"http://{hotspot_address}/login"), (status, lines, body[:500])
    portal_status, _, portal = device_http(device, f"{PORTAL_URL}/portal")
    assert portal_status == 200 and "OpenPiso WiFi" in portal, portal_status
    admin_status, _, admin_body = device_http(device, f"{PORTAL_URL}/admin/login")
    assert admin_status == 403 and "management network only" in admin_body, admin_status
    return {"upstream_status": status, "upstream_location": location,
            "portal_status": portal_status, "admin_status": admin_status}


def internet_without_time_credit(device: str, hotspot_address: str) -> dict:
    """A Device without Time Credit cannot ping 8.8.8.8, and google.com is redirected to the login. Its
    DNS is still answered (the HotSpot needs that to redirect), so the resolution is recorded, not refused."""
    assert not device_pings(device, "8.8.8.8"), "pinged 8.8.8.8 without Time Credit"
    status, lines, _ = device_http(device, "http://google.com/")
    location = header(lines, "location")
    assert status == 302 and location.startswith(f"http://{hotspot_address}/login"), (status, lines)
    return {"pings_8_8_8_8": False, "google_status": status, "google_location": location,
            "resolves_google": device_resolves(device, "google.com")}


def internet_with_time_credit(device: str) -> dict:
    """A logged-in Device pings 8.8.8.8 and resolves google.com through the Router's DNS."""
    assert device_pings(device, "8.8.8.8"), "could not ping 8.8.8.8 with Time Credit"
    resolved = device_resolves(device, "google.com")
    assert resolved == [GOOGLE_ADDRESS], resolved
    status, _, body = device_http(device, "http://google.com/")
    assert status == 200 and body.strip() == INTERNET_BODY, (status, body)
    return {"pings_8_8_8_8": True, "google_resolves_to": resolved, "google_status": status}


def vendo_hostname(mac: str) -> str:
    """The DHCP hostname a Vendo board is flashed with: opnw- and the MAC's last four hex digits."""
    return "opnw-" + mac.replace(":", "")[-4:].lower()


def vendo_post(container: str, frame: bytes, timeout: float) -> tuple[int, bytes]:
    """The Vendo in container POSTs a signed frame to the Controller from its own address and MAC;
    returns (status, body) as SimulatedVendo.send does, and raises URLError when no reply comes."""
    result = subprocess.run(["docker", "exec", "-i", container, "curl", "-sS", "-i", "--max-time", str(timeout),
                             "-H", "Content-Type: application/octet-stream", "--data-binary", "@-",
                             f"{PORTAL_URL}/vendo/messages"], input=frame, capture_output=True)
    if result.returncode != 0:
        raise urllib.error.URLError(f"{container}: curl exited {result.returncode}: {result.stderr.decode().strip()}")
    head, _, body = result.stdout.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def container_vendo(base, senders: dict[int, str]):
    """run.SimulatedVendo, sending from the Vendo's container once that Vendo has made first contact
    from it (senders maps a Vendo ID to its container); other Vendos send from the harness, as on libvirt."""
    class ContainerVendo(base):
        def send(self, frame: bytes):
            container = senders.get(self.id)
            return super().send(frame) if container is None else vendo_post(container, frame, self.timeout)
    return ContainerVendo


def sealed_from_internet(lab: Lab, device: str) -> dict:
    """Nothing in the lab reaches the real internet: a public address the stand-in does not answer is
    unroutable from every container except via the Router (Devices), and a logged-in Device gets neither
    ping, HTTP nor DNS beyond the stand-in, which forwards nothing."""
    routes = {}
    for container in lab.containers():
        output = sh("docker", "exec", container, "ip", "route", "get", REAL_INTERNET, check=False).strip()
        assert not output or re.match(rf"{REAL_INTERNET} via 10\.5\.\d+\.1 ", output), \
            f"{container} has a route to {REAL_INTERNET} that bypasses the Router: {output}"
        routes[container] = output or "unreachable"
    assert not device_pings(device, REAL_INTERNET), f"pinged {REAL_INTERNET}"
    assert not device_exec(device, f"curl -sS --max-time 5 -o /dev/null -w '%{{http_code}}' http://{REAL_INTERNET}/",
                           check=False).strip("0"), "HTTP reached the real internet"
    assert device_resolves(device, "example.com") == [], "resolved a name the stand-in does not serve"
    return {"route_to_" + REAL_INTERNET: routes, "device_ping": False, "device_http": False, "device_dns_example_com": []}


def memory_caps(lab: Lab) -> dict:
    """Every lab container has a hard memory cap (no swap beyond it): the Router 320 MB, the rest 128 MB."""
    caps = {}
    for container in lab.containers():
        memory, swap = sh("docker", "inspect", "-f", "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}",
                          container).split()
        want = ROUTER_MEMORY if "-router-" in container else OTHER_MEMORY
        assert int(memory) == int(swap) == want, (container, memory, swap)
        caps[container] = f"{want >> 20} MB"
    return caps


class ContainerProcess(subprocess.Popen):
    """`docker exec` running the Controller in its container. docker exec does not forward signals, so
    terminate() and kill() signal the Controller inside; docker exec then exits with its status."""

    def __init__(self, container: str, command: list[str], env: dict):
        self.container = container
        super().__init__(["docker", "exec", *(f"--env={name}" for name in env), container, *command],
                         env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def send_signal(self, signum):
        name = signal.Signals(signum).name.removeprefix("SIG")
        subprocess.run(["docker", "exec", self.container, "pkill", f"-{name}", "-x", "openpiso"], capture_output=True)


class ContainerFixture:
    """NonV7Router's address and requests, for the copy running in a container."""

    def __init__(self, address: str, process: subprocess.Popen):
        self.address = address
        self.process = process

    @property
    def requests(self) -> list[tuple[str, str]]:
        self.process.stdin.write("\n")
        self.process.stdin.flush()
        return [tuple(entry) for entry in json.loads(self.process.stdout.readline())]


class DockerLab:
    """The lab interface (see LibvirtLab) on a Docker lab. run is the loaded run.py module; `running`
    sets its BASE_URL, PORTAL_URL and CONTROLLER_HOST for the lab's published ports and Controller."""

    not_ported = frozenset({33, 34})  # tickets whose scenarios run.py skips here
    router_url = f"https://{ROUTER_WAN}"
    router_password = ROUTER_PASSWORD

    def __init__(self, run, binary: Path, keep: bool):
        self.run = run
        self.binary = binary.resolve()
        self.keep = keep
        self.lab = None
        self._ip = {}  # Device container -> its leased address, while it is on the WiFi
        self._lan = {}  # Vendo board and VLAN Device container -> its leased address
        self._vendo_senders = {}  # Vendo ID -> the container it made first contact from
        self._shared = None

    def _rest(self, path, method="GET", body=None):
        """Router REST as admin over plain HTTP, which works whatever state www-ssl is in."""
        return self.run.router_rest(self._router_http, "admin", ROUTER_PASSWORD, path, method, body, scheme="http")

    # Run setup and teardown.

    @contextlib.contextmanager
    def running(self, report):
        """The lab, up and ready for onboarding, for the length of the with block. SIGINT, SIGTERM and
        SIGHUP unwind, so the lab is removed however the run ends; SIGINT is set explicitly because a
        background job of a non-interactive shell inherits it ignored."""
        def interrupted(signum, _frame):
            raise KeyboardInterrupt(signal.Signals(signum).name)
        for signum in SIGNALS:
            signal.signal(signum, interrupted)
        started = time.monotonic()
        self._report = report  # vendo_report records where each Report came from
        chr_image = router_image()
        self.lab = Lab(self.binary, chr_image, lab_image())
        report.data["test_seam"] = "Controller binary via external HTTP against a Docker lab: stock CHR Router and Linux Devices"
        report.data["lab"] = {"name": self.lab.name, "chr_version": CHR_VERSION, "chr_image": str(chr_image),
                              "image": self.lab.image}
        try:
            try:
                self.lab.up()
                self._setup(report)
                yield
            except BaseException:
                report.capture_lab_failure()
                try:
                    report.data["lab_logs"] = self.lab.logs()
                except AssertionError as error:
                    report.data["lab_logs"] = str(error)
                raise
        finally:
            for signum in SIGNALS:
                signal.signal(signum, signal.SIG_IGN)  # a second signal must not cut the removal short
            report.data["lab"]["run_seconds"] = round(time.monotonic() - started, 1)
            if self.keep:
                self._print_kept()
            else:
                self.lab.down()
            report.data["lab"]["removed"] = not self.keep

    def _setup(self, report) -> None:
        self._router_http = self.lab.port("router", 8080)
        self.router_address = self.lab.port("router", 8443)
        self.controller = f"{self.lab.name}-controller-1"
        self.run.BASE_URL = "http://" + self.lab.port("controller", 18080)
        self.run.PORTAL_URL = PORTAL_URL
        self.run.CONTROLLER_HOST = self
        self.run.SimulatedVendo = container_vendo(self.run.SimulatedVendo, self._vendo_senders)

        def stock(path, method="GET", body=None):
            return self.run.router_rest(self._router_http, "admin", "", path, method, body, scheme="http")

        started = time.monotonic()
        deadline = started + 180
        while True:
            try:
                resource = stock("/system/resource")
                break
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                assert time.monotonic() < deadline, "Router REST did not answer on the published port"
                time.sleep(1)
        report.data["lab"]["router_rest_seconds"] = round(time.monotonic() - started, 1)
        assert resource["version"].startswith(CHR_VERSION), resource
        assert stock("/ip/hotspot") == []
        report.data["lab"]["stock_router"] = {"version": resource["version"], "admin_password": "empty", "hotspot_servers": 0}
        print("PASS router-is-stock-chr", flush=True)

        try:
            stock("/user/set", "POST", {"numbers": "admin", "password": ROUTER_PASSWORD})
        except urllib.error.HTTPError as error:
            # RouterOS ends the session whose own password just changed.
            if "Session closed" not in error.read().decode():
                raise
        rest = self._rest
        # The stock DHCP client on ether1 takes the stand-in's gateway and DNS; Devices resolve through the Router.
        rest("/ip/dns/set", "POST", {"allow-remote-requests": "yes"})
        # The HotSpot baseline the libvirt wizard created by hand, plus HotSpot servers on VLANs 10 and 20
        # (libvirt's vlan_up adds those). udhcpc does not renew (-q), so a lease outlives the run.
        for name, interface, subnet, pool in (("hs1", "ether2", "10.5.50", "10-10.5.50.200"),
                                              ("opnw-vlan10", "opnw-vlan10", "10.5.10", "100-10.5.10.200"),
                                              ("opnw-vlan20", "opnw-vlan20", "10.5.20", "100-10.5.20.200")):
            if interface != "ether2":
                rest("/interface/vlan", "PUT", {"name": interface, "interface": "ether2",
                                                "vlan-id": interface.removeprefix("opnw-vlan")})
            pool_name = "hs-pool" if name == "hs1" else name
            rest("/ip/address", "PUT", {"address": f"{subnet}.1/24", "interface": interface})
            rest("/ip/pool", "PUT", {"name": pool_name, "ranges": f"{subnet}.{pool}"})
            rest("/ip/dhcp-server", "PUT", {"name": "hs-dhcp" if name == "hs1" else name, "interface": interface,
                                            "address-pool": pool_name, "lease-time": "1d", "disabled": "no"})
            rest("/ip/dhcp-server/network", "PUT", {"address": f"{subnet}.0/24", "gateway": f"{subnet}.1",
                                                    "dns-server": f"{subnet}.1"})
            profile = "hs-prof" if name == "hs1" else name
            rest("/ip/hotspot/profile", "PUT", {"name": profile, "hotspot-address": f"{subnet}.1",
                                                "login-by": "cookie,http-pap"})
            rest("/ip/hotspot", "PUT", {"name": name, "interface": interface, "address-pool": pool_name,
                                        "profile": profile, "disabled": "no"})

    def start_controller(self, initialize: bool) -> ContainerProcess:
        """Start the Controller in its container (run.Controller calls this). Its data is the container's
        /data, so it survives restarts; the WAN gateway the harness arrives from is its admin network."""
        env = {"OPENPISO_ADMIN_PASSWORD": self.run.ADMIN_PASSWORD} if initialize else {}
        script = ('exec openpiso -listen 0.0.0.0:18080 -data /data -portal-url "$1" '
                  '-admin-network "$(cat /run/admin-network)"')
        return ContainerProcess(self.controller, ["sh", "-c", script, "sh", PORTAL_URL], env)

    def new_router_certificate(self) -> None:
        """Give the Router a fresh TLS certificate and bootstrap admin."""
        rest = self._rest
        rest("/ip/service/set", "POST", {"numbers": "www-ssl", "disabled": "yes", "certificate": "none"})
        # Removing a CA removes what it signed too, so look again after each removal.
        while old := [c for c in rest("/certificate") if c["name"].startswith("openpiso-e2e")]:
            rest("/certificate/" + old[0][".id"], "DELETE")

        def sign(name, **ca):
            # RouterOS signs in the background: the POST returns before the certificate can be used.
            rest("/certificate/sign", "POST", {"number": name, **ca})
            deadline = time.monotonic() + 60
            while not rest(f"/certificate?name={name}")[0].get("fingerprint"):
                assert time.monotonic() < deadline, f"Router did not finish signing {name}"
                time.sleep(0.5)

        rest("/certificate", "PUT", {"name": "openpiso-e2e-ca", "common-name": "openpiso-e2e-ca",
                                     "key-usage": "key-cert-sign,crl-sign"})
        sign("openpiso-e2e-ca")
        rest("/certificate", "PUT", {"name": "openpiso-e2e-server", "common-name": ROUTER_WAN,
                                     "subject-alt-name": f"IP:{ROUTER_WAN}", "key-usage": "tls-server"})
        sign("openpiso-e2e-server", ca="openpiso-e2e-ca")
        rest("/ip/service/set", "POST", {"numbers": "www-ssl", "certificate": "openpiso-e2e-server",
                                         "tls-version": "only-1.2", "disabled": "no"})
        host, port = self.router_address.split(":")
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection((host, int(port)), timeout=2) as raw:
                    with ssl._create_unverified_context().wrap_socket(raw, server_hostname=host):
                        break
            except OSError:
                assert time.monotonic() < deadline, "Router www-ssl did not become ready"
                time.sleep(0.5)
        self.remove_bootstrap_admin()
        rest("/user", "PUT", {"name": self.run.ROUTER_ADMIN, "group": "full", "password": self.run.ROUTER_ADMIN_PASSWORD})

    @contextlib.contextmanager
    def non_v7_router(self, module):
        """The RouterOS 6 fixture (module is the loaded non_v7_router.py), run in the internet stand-in:
        the host's firewall may drop what containers send to it. The image has no openssl, so the
        certificate is made here."""
        container, host = f"{self.lab.name}-internet-1", "10.77.7.254"
        with tempfile.TemporaryDirectory(prefix="openpiso-non-v7-") as directory:
            module.certificate(Path(directory), host)
            shutil.copy(module.__file__, directory)
            sh("docker", "cp", directory + "/.", f"{container}:/tmp/non-v7")
        fixture = subprocess.Popen(["docker", "exec", "-i", container, "python3", "-I", "/tmp/non-v7/non_v7_router.py",
                                    host, "/tmp/non-v7"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            address = fixture.stdout.readline().strip()
            assert address.startswith("https://"), "RouterOS 6 fixture did not start"
            yield ContainerFixture(address, fixture)
        finally:
            fixture.communicate("", timeout=10)

    def remove_bootstrap_admin(self) -> None:
        for user in self._rest(f"/user?name={self.run.ROUTER_ADMIN}"):
            self._rest("/user/" + user[".id"], "DELETE")

    def stop_router_rest(self) -> None:
        """The Controller loses the Router's REST (www-ssl); the harness keeps plain HTTP. Undo with start_router_rest."""
        self._rest("/ip/service/set", "POST", {"numbers": "www-ssl", "disabled": "yes"})

    def start_router_rest(self) -> None:
        self._rest("/ip/service/set", "POST", {"numbers": "www-ssl", "disabled": "no"})

    def start(self, evidence: dict) -> None:
        """Nothing to start or clean: the lab is new."""

    def reset_device(self):
        """Connect Device 1 with a fresh lease; returns the Router's lease rows for it."""
        self._lease(self._device(1))
        return self._rest(f"/ip/dhcp-server/lease?mac-address={DEVICE_MACS[1]}")

    def route_devices(self) -> None:
        """Devices reach the Controller with their own source IPs already: the Router routes the LAN."""

    def teardown_steps(self):
        return []  # the whole lab is removed

    def snapshot(self) -> dict:
        """Failure evidence from the Router and the Devices."""
        evidence = {"router": {path: self._rest(path) for path in
                               ("/ip/dhcp-server/lease", "/ip/hotspot/host", "/ip/hotspot/active")}}
        evidence["devices"] = {device: device_exec(device, "ip -o link show eth0; ip -o -4 addr; ip route", check=False)
                               for device in self.lab.containers() if "-device" in device or "board" in device}
        return evidence

    def own_scenarios(self, opener, report) -> None:
        """What only this lab checks, on fresh Devices with Vouchers of their own: a Device without Time
        Credit, the address the Controller sees, ping and DNS, the seal, memory caps, a VLAN HotSpot
        login, and what IPv6 on the LAN can do."""
        run, address = self.run, self.router_address

        def router(path, method="GET", body=None):
            return run.router_rest(address, "admin", ROUTER_PASSWORD, path, method, body)

        codes = run.voucher_codes(opener, run.generate_vouchers(opener, {"seconds": "120", "count": "2"}))
        device = self.lab.device("own-device", OWN_DEVICE_MAC)
        device_ip = self._lease(device)
        report.snapshot("device-without-time-credit-cannot-reach-internet", address, ROUTER_PASSWORD, opener,
                        **internet_without_time_credit(device, "10.5.50.1"))
        online = self.redeem_online(device_ip, codes[0])
        state = json.load(run.request(opener, "/admin/state"))
        time_credit = run.time_credit_for_device(state, OWN_DEVICE_MAC, device_ip)
        active = self.router_active(router, device_ip, OWN_DEVICE_MAC, "hs1", time_credit["id"])
        # The Controller binds the Time Credit to the address it saw the request come from.
        assert time_credit["ip"] == device_ip == active["address"], (time_credit, device_ip)
        report.snapshot("controller-saw-device-real-address", address, ROUTER_PASSWORD, opener,
                        device_lease_ip=device_ip, controller_time_credit_ip=time_credit["ip"],
                        controller_time_credit_mac=time_credit["mac"], router_active=active, **online)
        report.snapshot("device-with-time-credit-pings-and-resolves", address, ROUTER_PASSWORD, opener,
                        **internet_with_time_credit(device))
        report.snapshot("lab-sealed-from-real-internet", address, ROUTER_PASSWORD, opener,
                        **sealed_from_internet(self.lab, device))
        report.snapshot("containers-have-memory-caps", address, ROUTER_PASSWORD, opener, caps=memory_caps(self.lab))

        vlan_device = self.lab.device("vlan-device", VLAN_DEVICE_MAC)
        device_exec(vlan_device, "ip link add link eth0 name eth0.10 type vlan id 10 && ip link set eth0.10 up")
        vlan_ip = device_lease(vlan_device, "eth0.10")
        assert vlan_ip.startswith("10.5.10."), vlan_ip
        self._ip[vlan_device] = vlan_ip
        vlan_redirect = redirected_before_time_credit(vlan_device, "10.5.10.1")
        vlan_online = self.redeem_online(vlan_ip, codes[1])
        state = json.load(run.request(opener, "/admin/state"))
        vlan_credit = run.time_credit_for_device(state, VLAN_DEVICE_MAC, vlan_ip)
        vlan_active = self.router_active(router, vlan_ip, VLAN_DEVICE_MAC, "opnw-vlan10", vlan_credit["id"])
        report.snapshot("vlan-hotspot-server-logs-device-in", address, ROUTER_PASSWORD, opener,
                        device_ip=vlan_ip, router_active=vlan_active, redirect=vlan_redirect, **vlan_online)

        report.data["ipv6"] = self.ipv6_on_lan(router)
        print(f"IPv6: {report.data['ipv6']['result']}", flush=True)

    def redeem_online(self, device_ip: str, code: str) -> dict:
        """Redeem a Voucher as the Device and reach the upstream target."""
        result = self.fetch(device_ip, f"{PORTAL_URL}/portal/api/redeem", {"code": code})
        state = json.loads(result["data"])
        self.run.assert_message(state, "success", "online")
        assert "Set-Cookie: openpiso_time_credit=" in result["http-headers"], result["http-headers"]
        upstream = self.fetch(device_ip, UPSTREAM_URL, headers=False)["data"]
        assert upstream.strip() == INTERNET_BODY, upstream
        return {"portal_message": state["message"]["code"], "upstream_body": upstream.strip()}

    @staticmethod
    def router_active(router, ip: str, mac: str, server: str, time_credit_id: str) -> dict:
        active = router(f"/ip/hotspot/active?address={ip}")
        assert len(active) == 1, active
        entry = active[0]
        assert (entry["user"], entry["mac-address"].upper(), entry["server"]) == (f"op-{time_credit_id}", mac, server), entry
        return {key: entry[key] for key in ("user", "address", "mac-address", "server")}

    def ipv6_on_lan(self, router) -> dict:
        """What IPv6 on the LAN can do: link-local traffic between a Device and the Router, and whether the
        Router's IPv6 firewall sees a Device by MAC (what the Vendo isolation assertions rely on)."""
        finding: dict = {"how": "Docker sets disable_ipv6=1 on every endpoint of a network without IPv6; a global "
                                "--sysctl does not undo it, the per-endpoint driver option com.docker.network.endpoint."
                                "sysctls=net.ipv6.conf.IFNAME.disable_ipv6=0 does. Link-local only: no prefix or RA."}
        plain = self.lab.device("ipv6-default", "02:00:00:00:0C:21")
        finding["default_container_disable_ipv6"] = device_exec(plain, "cat /proc/sys/net/ipv6/conf/eth0/disable_ipv6").strip()
        device = self.lab.device("ipv6", "02:00:00:00:0C:22", ipv6=True)
        time.sleep(3)  # duplicate address detection on the link-local address
        device_addresses = device_exec(device, "ip -o -6 addr show dev eth0 scope link")
        finding["device_link_local"] = re.findall(r"inet6 (\S+)/", device_addresses)
        router_link_local = [entry["address"].split("/")[0] for entry in router("/ipv6/address?interface=ether2")]
        finding["router_link_local"] = router_link_local
        if not router_link_local or not finding["device_link_local"]:
            finding["result"] = "no link-local address on one side"
            return finding
        target = f"{router_link_local[0]}%eth0"
        ping = device_exec(device, f"ping -6 -c 3 -W 2 {target}", check=False)
        finding["device_pings_router"] = " 0% packet loss" in ping
        finding["router_neighbor_rows"] = len(router("/ipv6/neighbor?mac-address=02:00:00:00:0C:22"))
        rule = router("/ipv6/firewall/filter", "PUT", {"chain": "input", "src-mac-address": "02:00:00:00:0C:22",
                                                        "action": "drop", "comment": "openpiso-lab-ipv6-probe"})
        blocked = device_exec(device, f"ping -6 -c 3 -W 2 {target}", check=False)
        counters = router(f"/ipv6/firewall/filter/{rule['.id']}")
        router(f"/ipv6/firewall/filter/{rule['.id']}", "DELETE")
        finding["drop_rule_blocks_device"] = "100% packet loss" in blocked
        finding["drop_rule_packets"] = int(counters.get("packets", "0"))
        finding["global_address_on_device"] = bool(device_exec(device, "ip -o -6 addr show dev eth0 scope global"))
        finding["result"] = ("isolation assertions can run" if finding["device_pings_router"]
                             and finding["drop_rule_blocks_device"] and finding["drop_rule_packets"] > 0
                             else "isolation assertions cannot run")
        return finding

    def _print_kept(self) -> None:
        lines = [f"Lab {self.lab.name} left running (--keep)."]
        try:
            lines += [f"  Router REST:  http://{self.lab.port('router', 8080)}/rest and https://{self.lab.port('router', 8443)}/rest "
                      f"(admin / {ROUTER_PASSWORD}, or admin with no password if setup did not get that far)",
                      f"  Controller:   http://{self.lab.port('controller', 18080)}/admin (password {self.run.ADMIN_PASSWORD}); "
                      f"stopped at the end of a run, start it with docker exec {self.controller} openpiso ..."]
        except AssertionError as error:
            lines.append(f"  Published ports unavailable: {error}")
        lines += [f"  Container:    docker exec -it {name} sh" for name in self.lab.containers()]
        lines.append(f"  Remove:       {self.lab.remove_command()}")
        print("\n".join(lines), flush=True)

    # Devices: one container each, named by its leased address.

    def _device(self, number: int) -> str:
        """Device number's container, started on first use."""
        name = f"{self.lab.name}-device{number}"
        if name not in self.lab.containers():
            self.lab.device(f"device{number}", DEVICE_MACS[number])
        return name

    def _lease(self, device: str) -> str:
        """The Device joins the WiFi (again) with a fresh lease; returns its address."""
        device_exec(device, "ip link set eth0 up; ip -4 addr flush dev eth0; ip -4 route flush dev eth0")
        self._ip[device] = device_lease(device)
        return self._ip[device]

    def _leave(self, device: str) -> None:
        device_exec(device, "ip link set eth0 down")
        self._ip.pop(device, None)

    def _sender(self, device_ip: str) -> str:
        return next(device for device, ip in (*self._ip.items(), *self._lan.items()) if ip == device_ip)

    def device_ip(self, second: bool = False) -> str:
        return self._ip[self._device(2 if second else 1)]

    def second_device_mac(self) -> str:
        return DEVICE_MACS[2]

    def fetch(self, device_ip: str, url: str, form=None, cookie: str = "", headers: bool = True):
        """This Device fetches url, following redirects, POSTing form when given; returns {"data"}, with the
        reply's "http-headers" ("Name: value" joined by commas) when headers. Like RouterOS /tool/fetch, a
        non-2xx reply raises HTTPError 400 whose JSON "detail" names the status; unlike it, the error also
        keeps the Portal's JSON reply as portal_reply."""
        status, lines, body = device_http(self._sender(device_ip), url, form, cookie, follow=True)
        if not 200 <= status < 300:
            detail = {"error": 400, "message": "Bad Request", "detail": f"failure: Status {status}", "reply": body}
            error = urllib.error.HTTPError(url, 400, "Bad Request", email.message.Message(),
                                           io.BytesIO(json.dumps(detail).encode()))
            with contextlib.suppress(json.JSONDecodeError):
                error.portal_reply = json.loads(body)
            raise error
        row = {"data": body}
        if headers:
            row["http-headers"] = ",".join(lines)
        return row

    def fetch_when_ready(self, device_ip: str, url: str):
        """This Device GETs url once it can, retrying refusals for 30s; returns [the result row]."""
        deadline = time.monotonic() + 30
        while True:
            try:
                return [self.fetch(device_ip, url, headers=False)]
            except (urllib.error.HTTPError, AssertionError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def unreachable(self, device_ip: str, url: str) -> dict:
        """Assert this Device gets no reply from url within 3s; returns the evidence. The Router takes about
        a second to apply firewall rules just written (measured on this lab), so replies within 10s of the
        first attempt are retried and counted."""
        deadline = time.monotonic() + 10
        replied = 0
        while True:
            output = device_exec(self._sender(device_ip), f"curl -sS --max-time 3 -o /dev/null -w ' status=%{{http_code}}' "
                                                          f"{url} 2>&1; echo \" exit=$?\"", check=False).strip()
            if "status=000" in output and "exit=0" not in output:
                return {"url": url, "curl": output, "replies_before_rules_applied": replied}
            replied += 1
            assert time.monotonic() < deadline, output
            time.sleep(0.5)

    def internet(self, device_ip: str) -> tuple[str, str]:
        """Fetch from the Device for checks that expect internet; returns (target, body)."""
        return UPSTREAM_URL, self.fetch(device_ip, UPSTREAM_URL, headers=False)["data"]

    def ping(self, device_ip: str) -> None:
        """Real traffic from this Device to the HotSpot."""
        device_exec(self._sender(device_ip), "ping -c 1 -W 2 10.5.50.1", check=False)

    def stop_background_traffic(self) -> None:
        """No traffic generator runs here."""

    def redirect(self) -> tuple[str, str]:
        """Connect Device 1 alone; returns its (redirect page, Portal page) and asserts it cannot reach the admin."""
        device = self._device(1)
        for other in self._ip.keys() - {device}:
            self._leave(other)
        device_ip = self._ip.get(device) or self._lease(device)
        redirect = self.fetch(device_ip, UPSTREAM_URL, headers=False)["data"]
        portal = self.fetch(device_ip, f"{PORTAL_URL}/portal", headers=False)["data"]
        status, _, body = device_http(device, f"{PORTAL_URL}/admin/login")
        assert status == 403 and "management network only" in body, (status, body[:500])
        return redirect, portal

    def renew_lease(self, *, verify_portal: bool = True) -> None:
        """Give Device 1 a fresh lease after its paired (static) lease was deleted."""
        self._lease(self._device(1))
        if verify_portal:
            self.wait_device()

    def second_device_up(self) -> list[str]:
        """Connect Device 2 beside Device 1. Returns [Device 1 IP, Device 2 IP]."""
        devices = [self._device(1), self._device(2)]
        ips = [self._ip.get(device) or self._lease(device) for device in devices]
        assert ips[0] != ips[1]
        for ip in ips:
            self.wait_device(second=ip == ips[1])
        return ips

    def wait_device(self, second: bool = False, url=None) -> str:
        """Wait until this Device can fetch url (default the Portal); returns its IP."""
        device_ip = self.device_ip(second)
        deadline = time.monotonic() + 30
        while True:
            try:
                self.fetch(device_ip, url or PORTAL_URL + "/portal", headers=False)
                return device_ip
            except (urllib.error.HTTPError, AssertionError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def change_mac(self) -> str:
        """Device 1 changes MAC and renews; returns its new IP. Undo with restore_mac.

        It takes Device 2's MAC, so Device 2 leaves first."""
        first, second = self._device(1), self._device(2)
        self._leave(second)
        device_exec(first, f"ip link set eth0 down && ip link set eth0 address {DEVICE_MACS[2]}")
        return self._lease(first)

    def leave(self) -> None:
        """Device 1 leaves the WiFi. Undo with rejoin."""
        self._leave(self._device(1))

    def restore_mac(self) -> None:
        """Device 1 is back on its own MAC and Device 2 is back on the WiFi; safe to repeat."""
        first = self._device(1)
        if device_exec(first, "cat /sys/class/net/eth0/address").strip().upper() != DEVICE_MACS[1]:
            device_exec(first, f"ip link set eth0 down && ip link set eth0 address {DEVICE_MACS[1]}")
            self._ip.pop(first, None)
        for device in (first, f"{self.lab.name}-device2"):
            if device not in self._ip and device in self.lab.containers():
                self._lease(device)

    rejoin = restore_mac

    def share_ip(self, device_ip: str) -> None:
        """Device 1 leaves and Device 2 sends from device_ip, so the Router sees one IP on two MACs. Undo with unshare_ip."""
        second = self._device(2)
        self.leave()
        self._shared = device_ip
        device_exec(second, f"ip addr add {device_ip}/32 dev eth0")
        device_exec(second, f"ping -c 1 -W 2 -I {device_ip} 10.5.50.1", check=False)

    def unshare_ip(self) -> None:
        """Give the shared IP back to Device 1 and reconnect it; safe to repeat. Call wait_device for its IP."""
        if self._shared is not None:
            device_exec(self._device(2), f"ip addr del {self._shared}/32 dev eth0")
            self._shared = None
        self.rejoin()

    # Vendo boards: a container each, found by its leased address like a Device.

    def _board(self, suffix: str, mac: str, vlan: int = 0, hostname: str = "") -> dict:
        """Start a Vendo board on the LAN, tagged on vlan when given, and lease it an address sending
        hostname; returns {"mac", "ip"}."""
        board = self.lab.device(suffix, mac)
        interface = "eth0"
        if vlan:
            interface = f"eth0.{vlan}"
            device_exec(board, f"ip link add link eth0 name {interface} type vlan id {vlan} && ip link set {interface} up")
        self._lan[board] = device_lease(board, interface, hostname)
        if vlan:
            self._vlan_lease(board, mac, vlan)
        return {"mac": mac, "ip": self._lan[board]}

    def _vlan_lease(self, container: str, mac: str, vlan: int) -> None:
        """Assert the container's lease came from its VLAN's own DHCP server and record it as evidence.
        A MAC can hold a lease on each server (an earlier scenario used it on another VLAN), so the row
        is the one for the address the container leased."""
        rows = [row for row in self._rest(f"/ip/dhcp-server/lease?mac-address={mac}")
                if row["address"] == self._lan[container]]
        lease = {key: rows[0].get(key) for key in ("mac-address", "address", "server", "status")} if rows else {}
        assert len(rows) == 1 and lease["server"] == f"opnw-vlan{vlan}", (container, rows)
        self._report.data["lab"].setdefault("vlan_leases", []).append({"container": container, "vlan": vlan, **lease})

    def coin_board(self) -> dict:
        """The coin board beside Device 1 on the HotSpot LAN, hostname opnw-0c04; returns {"mac", "ip"}.
        Started on first use; later calls return the same board. Unpairing deletes the board's lease, and
        with this lab's three DHCP servers pairing needs one, so the board leases again then, as a real
        board renewing would."""
        mac = self.run._lab_module.COIN_BOARD_MAC
        board = f"{self.lab.name}-coin-board"
        if board not in self._lan:
            return self._board("coin-board", mac, hostname=vendo_hostname(mac))
        if not self._rest(f"/ip/dhcp-server/lease?mac-address={mac}"):
            device_exec(board, "ip -4 addr flush dev eth0; ip -4 route flush dev eth0")
            self._lan[board] = device_lease(board, hostname=vendo_hostname(mac))
        return {"mac": mac, "ip": self._lan[board]}

    def remove_coin_board(self) -> None:
        """The coin board goes with the lab (run-end cleanup calls this)."""

    def vendo_report(self, ip: str, frame: bytes) -> None:
        """The Vendo board at ip sends this signed Report to the Controller's PORTAL_URL from its own
        container, address and MAC. That Vendo's later Reports (run.SimulatedVendo) go from there too."""
        board = self._sender(ip)
        self._vendo_senders[int.from_bytes(frame[1:5])] = board
        deadline = time.monotonic() + 30
        while subprocess.run(["docker", "exec", board, "curl", "-sf", "--max-time", "3", "-o", "/dev/null",
                              f"{PORTAL_URL}/healthz"], capture_output=True).returncode != 0:
            assert time.monotonic() < deadline, f"Board at {ip} cannot reach the Controller"
            time.sleep(0.5)
        source = device_exec(board, "ip -o -4 addr show scope global; cat /sys/class/net/eth0/address")
        try:
            status, body = vendo_post(board, frame, self.run.HTTP_TIMEOUT)
        except urllib.error.URLError as error:
            # PairVendo clears existing connections, including the first report.
            # Assert rejection/acceptance through admin and Router observations.
            status, body = None, str(error.reason).encode()
        if status != 200:
            print("Board report:", status, body.decode(errors="replace").strip())
        self._report.data["lab"].setdefault("vendo_reports", []).append({
            "vendo_id": int.from_bytes(frame[1:5]), "container": board, "ip": ip,
            "source_address": re.findall(r"inet (\S+)/", source), "source_mac": source.split()[-1].upper(),
            "status": status})

    # Two HotSpot VLANs (10 and 20, both part of the lab), each with a Device; Vendo boards join either.

    def vlan_up(self) -> None:
        """Connect a Device on each VLAN. Undo with vlan_down."""
        for vlan, mac in VLAN_DEVICE_MACS.items():
            self._board(f"vlan{vlan}-device", mac, vlan)

    def vlan_hosts(self) -> list[dict]:
        """Each VLAN Device's Router host rows: [{"vlan", "host_rows"}]."""
        evidence = []
        for vlan in VLAN_DEVICE_MACS:
            device = f"{self.lab.name}-vlan{vlan}-device"
            device_exec(device, f"ping -c 1 -W 2 10.5.{vlan}.1", check=False)
            evidence.append({"vlan": vlan, "host_rows": self._rest(f"/ip/hotspot/host?address={self._lan[device]}")})
        return evidence

    def vlan_devices(self) -> dict[int, str]:
        """Each VLAN Device's IP: {vlan: Device IP}. Each container already routes out its own VLAN
        interface, so unlike libvirt there is no routing to set up."""
        return {vlan: self._lan[f"{self.lab.name}-vlan{vlan}-device"] for vlan in VLAN_DEVICE_MACS}

    def vlan_board(self, vlan: int, number: int, hostname=None) -> dict:
        """Vendo board number (1-3) on this VLAN, sending hostname over DHCP when given; returns {"mac", "ip"}."""
        return self._board(f"board{number}", self.run._lab_module.BOARD_MACS[number - 1], vlan, hostname or "")

    def vlan_leave(self, vlan: int) -> None:
        """This VLAN's Device leaves the WiFi: its VLAN interface goes down. Undo with vlan_rejoin."""
        device_exec(f"{self.lab.name}-vlan{vlan}-device", f"ip link set eth0.{vlan} down")

    def vlan_rejoin(self, vlan: int) -> None:
        """The VLAN Device's interface comes back up and leases again (down dropped its routes)."""
        device = f"{self.lab.name}-vlan{vlan}-device"
        device_exec(device, f"ip link set eth0.{vlan} up; ip -4 addr flush dev eth0.{vlan}")
        self._lan[device] = device_lease(device, f"eth0.{vlan}")
        self._vlan_lease(device, VLAN_DEVICE_MACS[vlan], vlan)

    def vlan_down(self) -> None:
        """Remove the VLAN Devices and boards; the VLAN HotSpots stay, they are part of the lab."""
        names = [f"{self.lab.name}-{suffix}" for suffix in
                 (*(f"vlan{vlan}-device" for vlan in VLAN_DEVICE_MACS), "board1", "board2", "board3")]
        if gone := [name for name in names if name in self.lab.containers()]:
            sh("docker", "rm", "-f", "-v", *gone)
        for name in names:
            self._lan.pop(name, None)
        for vendo_id, board in list(self._vendo_senders.items()):
            if board in names:
                del self._vendo_senders[vendo_id]
