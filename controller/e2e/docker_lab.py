#!/usr/bin/env python3
"""Docker lab E2E (issues #26, #28): one Device online in a throwaway, sealed Docker lab.

Creates a lab (stock RouterOS CHR under QEMU, the Controller binary, an internet stand-in, Linux
Devices), onboards the Router from zero over REST, takes one Device online with a Voucher, checks what
it can reach with and without Time Credit, logs a second Device in on a VLAN HotSpot server, records
what IPv6 on the LAN can do, writes the report and removes the lab however the run ends (--keep leaves
it up). The pinned CHR image is downloaded, verified and cached on first use.
Reuses run.py's REST, admin request, Voucher and Report helpers; everything lab-specific is here.
"""

import argparse
import fcntl
import hashlib
import http.cookiejar
import importlib.util
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

_run_spec = importlib.util.spec_from_file_location("e2e_run", Path(__file__).with_name("run.py"))
e2e = importlib.util.module_from_spec(_run_spec)
_run_spec.loader.exec_module(e2e)

ROOT = e2e.ROOT
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
DEVICE_MAC = "02:00:00:00:0C:01"
VLAN_DEVICE_MAC = "02:00:00:00:0C:11"


def sh(*command: str, check: bool = True, env=None) -> str:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(command)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
    return result.stdout


def verify_router_image(path: Path, sha256: str, what: str) -> None:
    with path.open("rb") as file:
        actual = hashlib.file_digest(file, "sha256").hexdigest()
    if actual != sha256:
        raise SystemExit(f"Checksum mismatch: {what} RouterOS CHR {CHR_VERSION} image {path} has sha256 {actual}, "
                         f"expected {sha256} (CHR_SHA256 in controller/e2e/docker_lab.py). Not using it.")


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
                verify_router_image(extracted, sha256, "downloaded")
                os.replace(extracted, image)
    verify_router_image(image, sha256, "cached")
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
        self.env = {"LAB": self.name, "LAB_IMAGE": image, "CHR_IMAGE": str(chr_image),
                    "CONTROLLER_BINARY": str(binary), "ADMIN_PASSWORD": e2e.ADMIN_PASSWORD}

    def compose(self, *args: str) -> str:
        return sh("docker", "compose", "-f", str(LAB_DIR / "compose.yml"), *args, env={**os.environ, **self.env})

    def up(self) -> None:
        self.compose("up", "-d")

    def port(self, service: str, port: int) -> str:
        return self.compose("port", service, str(port)).strip()

    def device(self, suffix: str, mac: str, ipv6: bool = False) -> str:
        """Start a Device on the LAN with its own MAC; it drops Docker's address before any lease."""
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


def device_lease(device: str, interface: str = "eth0") -> str:
    """Take a DHCP lease from the Router and return the Device's address. udhcpc's script cannot `mv` its
    resolv.conf over Docker's bind-mounted one and leaves it as resolv.conf.<pid>; copy that in place so
    the Device resolves through the DNS server its lease named."""
    device_exec(device, f"udhcpc -i {interface} -q -n -t 20 -T 2 && for f in /etc/resolv.conf.*; do "
                        "cat $f > /etc/resolv.conf && rm $f; done")
    output = device_exec(device, f"ip -o -4 addr show dev {interface}")
    return re.search(r"inet (\S+)/", output).group(1)


def device_http(device: str, url: str, form=None) -> tuple[int, dict, str]:
    """An HTTP request as the Device: status, lower-cased headers (repeats joined by newline), full body."""
    command = ["docker", "exec", device, "curl", "-sS", "-i", "--max-time", "10"]
    if form is not None:
        command += ["--data", urllib.parse.urlencode(form)]
    raw = sh(*command, url)  # text mode has already turned CRLF into LF
    head, _, body = raw.partition("\n\n")
    lines = head.split("\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        key = key.strip().lower()
        headers[key] = (headers[key] + "\n" if key in headers else "") + value.strip()
    return int(lines[0].split()[1]), headers, body


def wait_router(http_address: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return e2e.router_rest(http_address, "admin", "", "/system/resource", scheme="http")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(1)
    raise AssertionError("Router REST did not answer on the published port")


def setup_router(http_address: str, https_address: str) -> None:
    """Everything onboarding needs that a stock CHR lacks, set over REST as admin with an empty password."""
    def stock(path, method="GET", body=None):
        return e2e.router_rest(http_address, "admin", "", path, method, body, scheme="http")

    try:
        stock("/user/set", "POST", {"numbers": "admin", "password": ROUTER_PASSWORD})
    except urllib.error.HTTPError as error:
        # RouterOS ends the session whose own password just changed.
        if "Session closed" not in error.read().decode():
            raise

    def rest(path, method="GET", body=None):
        return e2e.router_rest(http_address, "admin", ROUTER_PASSWORD, path, method, body, scheme="http")

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
    host, port = https_address.split(":")
    deadline = time.monotonic() + 30
    while True:
        try:
            with socket.create_connection((host, int(port)), timeout=2) as raw:
                with ssl._create_unverified_context().wrap_socket(raw, server_hostname=host):
                    break
        except OSError:
            if time.monotonic() > deadline:
                raise AssertionError("Router www-ssl did not become ready") from None
            time.sleep(0.5)
    # The stock DHCP client on ether1 takes the stand-in's gateway and DNS; Devices resolve through the Router.
    rest("/ip/dns/set", "POST", {"allow-remote-requests": "yes"})
    rest("/user", "PUT", {"name": e2e.ROUTER_ADMIN, "group": "full", "password": e2e.ROUTER_ADMIN_PASSWORD})

    # The HotSpot baseline the libvirt wizard created by hand, plus a HotSpot server on VLAN 10.
    for name, interface, subnet, pool in (("hs1", "ether2", "10.5.50", "10-10.5.50.200"),
                                          ("opnw-vlan10", "opnw-vlan10", "10.5.10", "100-10.5.10.200")):
        if interface != "ether2":
            rest("/interface/vlan", "PUT", {"name": interface, "interface": "ether2", "vlan-id": "10"})
        pool_name = "hs-pool" if name == "hs1" else name
        rest("/ip/address", "PUT", {"address": f"{subnet}.1/24", "interface": interface})
        rest("/ip/pool", "PUT", {"name": pool_name, "ranges": f"{subnet}.{pool}"})
        rest("/ip/dhcp-server", "PUT", {"name": "hs-dhcp" if name == "hs1" else name, "interface": interface,
                                        "address-pool": pool_name, "lease-time": "10m", "disabled": "no"})
        rest("/ip/dhcp-server/network", "PUT", {"address": f"{subnet}.0/24", "gateway": f"{subnet}.1",
                                                "dns-server": f"{subnet}.1"})
        profile = "hs-prof" if name == "hs1" else name
        rest("/ip/hotspot/profile", "PUT", {"name": profile, "hotspot-address": f"{subnet}.1",
                                            "login-by": "cookie,http-pap"})
        rest("/ip/hotspot", "PUT", {"name": name, "interface": interface, "address-pool": pool_name,
                                    "profile": profile, "disabled": "no"})


def onboard(opener, router) -> dict:
    response = e2e.request(opener, "/admin/router/probe", "POST", {
        "address": f"https://{ROUTER_WAN}", "username": e2e.ROUTER_ADMIN, "password": e2e.ROUTER_ADMIN_PASSWORD})
    page = response.read().decode()
    assert response.status == 200, page
    fingerprint = re.search(r"SHA256:(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", page)
    assert fingerprint, page
    assert fingerprint.group(0).removeprefix("SHA256:").replace(":", "") == \
        router("/certificate?name=openpiso-e2e-server")[0]["fingerprint"].upper()
    response = e2e.request(opener, "/admin/router/confirm", "POST")
    page = response.read().decode()
    assert response.status == 200 and "Router connected" in page, page
    state = json.load(e2e.request(opener, "/admin/state"))["router"]
    assert state["status"] == "ready" and state["address"] == f"https://{ROUTER_WAN}", state
    return state


def redirected_before_time_credit(device: str, hotspot_address: str) -> dict:
    """The Device has no Time Credit: upstream is redirected to the Router's login, the Portal is walled in."""
    status, headers, body = device_http(device, UPSTREAM_URL)
    assert status == 302 and headers.get("location", "").startswith(f"http://{hotspot_address}/login"), \
        (status, headers, body[:500])
    portal_status, _, portal = device_http(device, f"{PORTAL_URL}/portal")
    assert portal_status == 200 and "OpenPiso WiFi" in portal, portal_status
    admin_status, _, admin_body = device_http(device, f"{PORTAL_URL}/admin/login")
    assert admin_status == 403 and "management network only" in admin_body, admin_status
    return {"upstream_status": status, "upstream_location": headers["location"],
            "portal_status": portal_status, "admin_status": admin_status}


def device_pings(device: str, address: str) -> bool:
    return " 0% packet loss" in device_exec(device, f"ping -c 3 -W 2 {address}", check=False)


def device_resolves(device: str, name: str) -> list[str]:
    """Addresses the Device's resolver (the DNS server its lease named) returns for `name`."""
    output = device_exec(device, f"nslookup -type=a {name}", check=False)
    return re.findall(r"Address: (\d+\.\d+\.\d+\.\d+)", output.partition("Name:")[2])


def internet_without_time_credit(device: str, hotspot_address: str) -> dict:
    """A Device without Time Credit cannot ping 8.8.8.8, and google.com is redirected to the login. Its
    DNS is still answered (the HotSpot needs that to redirect), so the resolution is recorded, not refused."""
    assert not device_pings(device, "8.8.8.8"), "pinged 8.8.8.8 without Time Credit"
    status, headers, _ = device_http(device, "http://google.com/")
    assert status == 302 and headers.get("location", "").startswith(f"http://{hotspot_address}/login"), \
        (status, headers)
    return {"pings_8_8_8_8": False, "google_status": status, "google_location": headers["location"],
            "resolves_google": device_resolves(device, "google.com")}


def internet_with_time_credit(device: str) -> dict:
    """A logged-in Device pings 8.8.8.8 and resolves google.com through the Router's DNS."""
    assert device_pings(device, "8.8.8.8"), "could not ping 8.8.8.8 with Time Credit"
    resolved = device_resolves(device, "google.com")
    assert resolved == [GOOGLE_ADDRESS], resolved
    status, _, body = device_http(device, "http://google.com/")
    assert status == 200 and body.strip() == INTERNET_BODY, (status, body)
    return {"pings_8_8_8_8": True, "google_resolves_to": resolved, "google_status": status}


def sealed_from_internet(lab: "Lab", device: str) -> dict:
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


def memory_caps(lab: "Lab") -> dict:
    """Every lab container has a hard memory cap (no swap beyond it): the Router 320 MB, the rest 128 MB."""
    caps = {}
    for container in lab.containers():
        memory, swap = sh("docker", "inspect", "-f", "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}",
                          container).split()
        want = ROUTER_MEMORY if "-router-" in container else OTHER_MEMORY
        assert int(memory) == int(swap) == want, (container, memory, swap)
        caps[container] = f"{want >> 20} MB"
    return caps


def redeem_online(device: str, code: str) -> dict:
    """Redeem a Voucher as the Device and reach the upstream target."""
    status, headers, body = device_http(device, f"{PORTAL_URL}/portal/api/redeem", {"code": code})
    assert status == 200, (status, body)
    state = json.loads(body)
    e2e.assert_message(state, "success", "online")
    assert "openpiso_time_credit=" in headers.get("set-cookie", ""), headers
    upstream_status, _, upstream = device_http(device, UPSTREAM_URL)
    assert upstream_status == 200 and upstream.strip() == INTERNET_BODY, (upstream_status, upstream)
    return {"redeem_status": status, "portal_message": state["message"]["code"],
            "upstream_status": upstream_status, "upstream_body": upstream.strip()}


def router_active(router, ip: str, mac: str, server: str, time_credit_id: str) -> dict:
    active = router(f"/ip/hotspot/active?address={ip}")
    assert len(active) == 1, active
    entry = active[0]
    assert (entry["user"], entry["mac-address"].upper(), entry["server"]) == (f"op-{time_credit_id}", mac, server), entry
    return {key: entry[key] for key in ("user", "address", "mac-address", "server")}


def ipv6_on_lan(router, lab: Lab) -> dict:
    """What IPv6 on the LAN can do: link-local traffic between a Device and the Router, and whether the
    Router's IPv6 firewall sees a Device by MAC (what the Vendo isolation assertions rely on)."""
    finding: dict = {"how": "Docker sets disable_ipv6=1 on every endpoint of a network without IPv6; a global "
                            "--sysctl does not undo it, the per-endpoint driver option com.docker.network.endpoint."
                            "sysctls=net.ipv6.conf.IFNAME.disable_ipv6=0 does. Link-local only: no prefix or RA."}
    plain = lab.device("ipv6-default", "02:00:00:00:0C:21")
    finding["default_container_disable_ipv6"] = device_exec(plain, "cat /proc/sys/net/ipv6/conf/eth0/disable_ipv6").strip()
    device = lab.device("ipv6", "02:00:00:00:0C:22", ipv6=True)
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


def journey(lab: Lab, report) -> None:
    router_http = lab.port("router", 8080)
    router_https = lab.port("router", 8443)
    e2e.BASE_URL = "http://" + lab.port("controller", 18080)
    started = time.monotonic()
    resource = wait_router(router_http)
    report.data["lab"]["router_rest_seconds"] = round(time.monotonic() - started, 1)
    assert resource["version"].startswith(CHR_VERSION), resource
    assert e2e.router_rest(router_http, "admin", "", "/ip/hotspot", scheme="http") == []
    report.data["lab"]["stock_router"] = {"version": resource["version"], "admin_password": "empty", "hotspot_servers": 0}
    print("PASS router-is-stock-chr", flush=True)

    setup_router(router_http, router_https)

    def router(path, method="GET", body=None):
        return e2e.router_rest(router_https, "admin", ROUTER_PASSWORD, path, method, body)

    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    response = e2e.request(opener, "/admin/login", "POST", {"password": e2e.ADMIN_PASSWORD})
    assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/dashboard", response.status
    router_state = onboard(opener, router)
    report.snapshot("router-onboarded-from-stock-over-rest", router_https, ROUTER_PASSWORD, opener, router=router_state)

    codes = e2e.voucher_batch(opener, report, router_https, ROUTER_PASSWORD)

    device = lab.device("device", DEVICE_MAC)
    device_ip = device_lease(device)
    lease = router(f"/ip/dhcp-server/lease?mac-address={DEVICE_MAC}")
    assert len(lease) == 1 and lease[0]["address"] == device_ip and lease[0]["server"] == "hs-dhcp", lease
    redirect = redirected_before_time_credit(device, "10.5.50.1")
    report.snapshot("device-leased-and-redirected-before-time-credit", router_https, ROUTER_PASSWORD, opener,
                    device_ip=device_ip, device_mac=DEVICE_MAC, lease_server=lease[0]["server"], **redirect)
    report.snapshot("device-without-time-credit-cannot-reach-internet", router_https, ROUTER_PASSWORD, opener,
                    **internet_without_time_credit(device, "10.5.50.1"))

    online = redeem_online(device, codes[0])
    state = json.load(e2e.request(opener, "/admin/state"))
    time_credit = e2e.time_credit_for_device(state, DEVICE_MAC, device_ip)
    active = router_active(router, device_ip, DEVICE_MAC, "hs1", time_credit["id"])
    report.snapshot("voucher-redeemed-device-online-router-lists-active", router_https, ROUTER_PASSWORD, opener,
                    router_active=active, **online)
    # The Controller binds the Time Credit to the address it saw the request come from.
    assert time_credit["ip"] == device_ip == active["address"], (time_credit, device_ip)
    report.snapshot("controller-saw-device-real-address", router_https, ROUTER_PASSWORD, opener,
                    device_lease_ip=device_ip, controller_time_credit_ip=time_credit["ip"],
                    controller_time_credit_mac=time_credit["mac"])
    report.snapshot("device-with-time-credit-pings-and-resolves", router_https, ROUTER_PASSWORD, opener,
                    **internet_with_time_credit(device))
    report.snapshot("lab-sealed-from-real-internet", router_https, ROUTER_PASSWORD, opener,
                    **sealed_from_internet(lab, device))
    report.snapshot("containers-have-memory-caps", router_https, ROUTER_PASSWORD, opener, caps=memory_caps(lab))

    vlan_device = lab.device("vlan-device", VLAN_DEVICE_MAC)
    device_exec(vlan_device, "ip link add link eth0 name eth0.10 type vlan id 10 && ip link set eth0.10 up")
    vlan_ip = device_lease(vlan_device, "eth0.10")
    assert vlan_ip.startswith("10.5.10."), vlan_ip
    vlan_redirect = redirected_before_time_credit(vlan_device, "10.5.10.1")
    vlan_online = redeem_online(vlan_device, codes[1])
    state = json.load(e2e.request(opener, "/admin/state"))
    vlan_credit = e2e.time_credit_for_device(state, VLAN_DEVICE_MAC, vlan_ip)
    vlan_active = router_active(router, vlan_ip, VLAN_DEVICE_MAC, "opnw-vlan10", vlan_credit["id"])
    report.snapshot("vlan-hotspot-server-logs-device-in", router_https, ROUTER_PASSWORD, opener,
                    device_ip=vlan_ip, router_active=vlan_active, redirect=vlan_redirect, **vlan_online)

    report.data["ipv6"] = ipv6_on_lan(router, lab)
    print(f"IPv6: {report.data['ipv6']['result']}", flush=True)


def print_kept(lab: Lab) -> None:
    lines = [f"Lab {lab.name} left running (--keep)."]
    try:
        lines += [f"  Router REST:  http://{lab.port('router', 8080)}/rest and https://{lab.port('router', 8443)}/rest "
                  f"(admin / {ROUTER_PASSWORD}, or admin with no password if setup did not get that far)",
                  f"  Controller:   http://{lab.port('controller', 18080)}/admin (password {e2e.ADMIN_PASSWORD})"]
    except AssertionError as error:
        lines.append(f"  Published ports unavailable: {error}")
    lines += [f"  Container:    docker exec -it {name} sh" for name in lab.containers()]
    lines.append(f"  Remove:       {lab.remove_command()}")
    print("\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True, help="Controller binary under test (linux/amd64)")
    parser.add_argument("--report", type=Path, default=ROOT / "controller/e2e/reports/docker-tracer.json")
    parser.add_argument("--keep", action="store_true", help="leave the lab running and print how to reach and remove it")
    args = parser.parse_args()
    chr_image = router_image()
    e2e.HTTP_TIMEOUT = 30
    e2e.load_lab_env = lambda: {"CHR_PASSWORD": ROUTER_PASSWORD}  # Report.finish redacts this secret
    report = e2e.Report(str(args.binary), args.report.resolve())
    report.data["scenario"] = "26-docker-lab-tracer"
    report.data["test_seam"] = "Controller binary via external HTTP against a Docker lab: stock CHR Router and Linux Devices"
    lab = Lab(args.binary.resolve(), chr_image, lab_image())
    report.data["lab"] = {"name": lab.name, "chr_version": CHR_VERSION, "chr_image": str(chr_image), "image": lab.image}
    started = time.monotonic()

    # SIGTERM and SIGHUP unwind like Ctrl-C, so the lab is removed however the run ends.
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(signal.Signals(signum).name)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        try:
            lab.up()
            journey(lab, report)
        except BaseException:
            try:
                report.data["lab_logs"] = lab.logs()
            except AssertionError as error:
                report.data["lab_logs"] = str(error)
            raise
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, signal.SIG_IGN)  # a second signal must not cut the removal short
            report.data["lab"]["journey_seconds"] = round(time.monotonic() - started, 1)
            if args.keep:
                print_kept(lab)
            else:
                lab.down()
            report.data["lab"]["removed"] = not args.keep
    except BaseException as error:
        report.finish("fail", f"{type(error).__name__}: {error}")
        raise
    report.data["lab"]["total_seconds"] = round(time.monotonic() - started, 1)
    report.finish("pass")


if __name__ == "__main__":
    main()
