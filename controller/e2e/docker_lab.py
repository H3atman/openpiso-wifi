#!/usr/bin/env python3
"""Issue #26 tracer bullet: one Device online in a throwaway Docker lab.

Creates a lab (stock RouterOS CHR under QEMU, the Controller binary, Linux Devices), onboards the
Router from zero over REST, takes one Device online with a Voucher, logs a second Device in on a
VLAN HotSpot server, records what IPv6 on the LAN can do, writes the report and removes the lab.
Reuses run.py's REST, admin request, Voucher and Report helpers; everything lab-specific is here.
"""

import argparse
import http.cookiejar
import importlib.util
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_run_spec = importlib.util.spec_from_file_location("e2e_run", Path(__file__).with_name("run.py"))
e2e = importlib.util.module_from_spec(_run_spec)
_run_spec.loader.exec_module(e2e)

ROOT = e2e.ROOT
LAB_DIR = ROOT / "lab/docker"
CHR_VERSION = "7.23.8"
CHR_IMAGE = Path.home() / f".cache/openpiso/chr/chr-{CHR_VERSION}.img"
ROUTER_PASSWORD = "docker-lab-router-admin"  # set by the harness on the stock Router's empty admin
ROUTER_WAN = "10.77.7.2"
PORTAL_URL = "http://10.77.7.1:18080"
UPSTREAM_URL = "http://10.77.7.254/"  # a non-walled WAN address standing in for the internet
DEVICE_MAC = "02:00:00:00:0C:01"
VLAN_DEVICE_MAC = "02:00:00:00:0C:11"


def sh(*command: str, check: bool = True, env=None) -> str:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(command)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
    return result.stdout


class Lab:
    """One lab: its compose project, image and Device containers all carry the `name` prefix."""

    def __init__(self, binary: Path):
        self.name = "opnwlab-" + secrets.token_hex(3)
        self.image = f"{self.name}-image"
        self.devices: list[str] = []
        self.env = {"LAB": self.name, "LAB_IMAGE": self.image, "CHR_IMAGE": str(CHR_IMAGE),
                    "CONTROLLER_BINARY": str(binary), "ADMIN_PASSWORD": e2e.ADMIN_PASSWORD}

    def compose(self, *args: str) -> str:
        return sh("docker", "compose", "-f", str(LAB_DIR / "compose.yml"), *args, env={**os.environ, **self.env})

    def up(self) -> None:
        sh("docker", "build", "-q", "-t", self.image, str(LAB_DIR))
        self.compose("up", "-d")

    def port(self, service: str, port: int) -> str:
        return self.compose("port", service, str(port)).strip()

    def device(self, suffix: str, mac: str, ipv6: bool = False) -> str:
        """Start a Device on the LAN with its own MAC; it drops Docker's address before any lease."""
        name = f"{self.name}-{suffix}"
        network = f"name={self.name}-lan"
        if ipv6:
            network += ",driver-opt=com.docker.network.endpoint.sysctls=net.ipv6.conf.IFNAME.disable_ipv6=0"
        self.devices.append(name)
        sh("docker", "run", "-d", "--name", name, "--cap-add", "NET_ADMIN", "--network", network,
           "--mac-address", mac, self.image, "sleep", "infinity")
        sh("docker", "exec", name, "sh", "-c", "ip -4 addr flush dev eth0; ip -4 route flush dev eth0")
        return name

    def logs(self) -> dict:
        return {service: self.compose("logs", "--no-color", "--tail", "40", service)[-4000:]
                for service in ("router", "controller")}

    def down(self) -> None:
        for name in self.devices:
            sh("docker", "rm", "-f", name, check=False)
        self.compose("down", "-v", "--remove-orphans", "-t", "2")
        sh("docker", "image", "rm", "-f", self.image, check=False)


def device_exec(device: str, script: str, check: bool = True) -> str:
    return sh("docker", "exec", device, "sh", "-c", script, check=check)


def device_lease(device: str, interface: str = "eth0") -> str:
    """Take a DHCP lease from the Router and return the Device's address."""
    device_exec(device, f"udhcpc -i {interface} -q -n -t 20 -T 2")
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

    rest("/certificate", "PUT", {"name": "openpiso-e2e-ca", "common-name": "openpiso-e2e-ca",
                                 "key-usage": "key-cert-sign,crl-sign"})
    rest("/certificate/sign", "POST", {"number": "openpiso-e2e-ca"})
    rest("/certificate", "PUT", {"name": "openpiso-e2e-server", "common-name": ROUTER_WAN,
                                 "subject-alt-name": f"IP:{ROUTER_WAN}", "key-usage": "tls-server"})
    rest("/certificate/sign", "POST", {"number": "openpiso-e2e-server", "ca": "openpiso-e2e-ca"})
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


def redeem_online(device: str, code: str) -> dict:
    """Redeem a Voucher as the Device and reach the upstream target."""
    status, headers, body = device_http(device, f"{PORTAL_URL}/portal/api/redeem", {"code": code})
    assert status == 200, (status, body)
    state = json.loads(body)
    e2e.assert_message(state, "success", "online")
    assert "openpiso_time_credit=" in headers.get("set-cookie", ""), headers
    upstream_status, _, upstream = device_http(device, UPSTREAM_URL)
    assert upstream_status == 200 and upstream.strip() == "openpiso-lab-upstream", (upstream_status, upstream)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True, help="Controller binary under test (linux/amd64)")
    parser.add_argument("--report", type=Path, default=ROOT / "controller/e2e/reports/docker-tracer.json")
    args = parser.parse_args()
    assert CHR_IMAGE.is_file(), f"download RouterOS CHR {CHR_VERSION} to {CHR_IMAGE} first"
    e2e.HTTP_TIMEOUT = 30
    e2e.load_lab_env = lambda: {"CHR_PASSWORD": ROUTER_PASSWORD}  # Report.finish redacts this secret
    report = e2e.Report(str(args.binary), args.report.resolve())
    report.data["scenario"] = "26-docker-lab-tracer"
    report.data["test_seam"] = "Controller binary via external HTTP against a Docker lab: stock CHR Router and Linux Devices"
    lab = Lab(args.binary.resolve())
    report.data["lab"] = {"name": lab.name, "chr_version": CHR_VERSION}
    started = time.monotonic()
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
            report.data["lab"]["journey_seconds"] = round(time.monotonic() - started, 1)
            lab.down()
            report.data["lab"]["removed"] = True
    except BaseException as error:
        report.finish("fail", f"{type(error).__name__}: {error}")
        raise
    report.data["lab"]["total_seconds"] = round(time.monotonic() - started, 1)
    report.finish("pass")


if __name__ == "__main__":
    main()
