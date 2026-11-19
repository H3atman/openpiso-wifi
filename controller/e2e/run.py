#!/usr/bin/env python3
"""Issue #6 end-to-end scenario.

The native or ARM-guest Controller is exercised through HTTP. Router assertions
use the real chr-lab Router; later ticket scenarios extend this same harness.
"""

import argparse
import base64
import concurrent.futures
import csv
import threading
import datetime
import hashlib
import html
import http.cookiejar
import importlib.util
import io
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:18080"
PORTAL_URL = "http://192.168.122.1:18080"
PORTAL_CSP = "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"  # docs/portal-contract.md
ARM_VM = None
HTTP_TIMEOUT = 3
ADMIN_PASSWORD = "controller-e2e-password"
ROUTER_ADMIN = "openpiso-bootstrap"
ROUTER_ADMIN_PASSWORD = "Tést& = + # issue6"
ROOT = Path(__file__).resolve().parents[2]
# python -I intentionally omits the script directory from sys.path.
_safety_spec = importlib.util.spec_from_file_location("lab_safety", Path(__file__).with_name("lab_safety.py"))
_safety_module = importlib.util.module_from_spec(_safety_spec)
_safety_spec.loader.exec_module(_safety_module)
_vendo_spec = importlib.util.spec_from_file_location("simulated_vendo", Path(__file__).with_name("simulated_vendo.py"))
_vendo_module = importlib.util.module_from_spec(_vendo_spec)
_vendo_spec.loader.exec_module(_vendo_module)
SimulatedVendo = _vendo_module.SimulatedVendo
_flash_spec = importlib.util.spec_from_file_location("flash_pairing", Path(__file__).with_name("flash_pairing.py"))
_flash_module = importlib.util.module_from_spec(_flash_spec)
_flash_spec.loader.exec_module(_flash_module)
_browser_spec = importlib.util.spec_from_file_location("browser_portal", Path(__file__).with_name("browser_portal.py"))
_browser_module = importlib.util.module_from_spec(_browser_spec)
_browser_spec.loader.exec_module(_browser_module)
_lab_spec = importlib.util.spec_from_file_location("e2e_lab", ROOT / "lab/chr/e2e_lab.py")
_lab_module = importlib.util.module_from_spec(_lab_spec)
_lab_spec.loader.exec_module(_lab_module)
_vlan_spec = importlib.util.spec_from_file_location("vlan_lab", ROOT / "lab/chr/vlan_lab.py")
_vlan_module = importlib.util.module_from_spec(_vlan_spec)
_vlan_spec.loader.exec_module(_vlan_module)
_hotspot_spec = importlib.util.spec_from_file_location("hotspot_lab", ROOT / "lab/chr/hotspot_lab.py")
_hotspot_module = importlib.util.module_from_spec(_hotspot_spec)
_hotspot_spec.loader.exec_module(_hotspot_module)


def load_lab_env() -> dict[str, str]:
    path = ROOT / "lab/chr/.env"
    if not path.is_file():
        common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                check=True, capture_output=True, text=True).stdout.strip()
        path = Path(common).parent / "lab/chr/.env"
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip("'\"")
    print(f"Loaded lab credentials from {path}", flush=True)
    return values


def router_address(domain="chr-lab") -> str:
    output = subprocess.run(
        ["virsh", "-c", "qemu:///system", "domifaddr", domain],
        timeout=10,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"(192\.168\.122\.\d+)/24", output)
    if not match:
        raise AssertionError(f"{domain} has no management address")
    return match.group(1)


def start_client(password: str) -> str:
    state = subprocess.run(
        ["virsh", "-c", "qemu:///system", "domstate", "chr-client"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if state != "running":
        subprocess.run(["virsh", "-c", "qemu:///system", "start", "chr-client"], check=True, capture_output=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        output = subprocess.run(
            ["virsh", "-c", "qemu:///system", "domifaddr", "chr-client"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        match = re.search(r"(192\.168\.122\.\d+)/24", output)
        if match:
            address = match.group(1)
            try:
                with urllib.request.urlopen(f"http://{address}/", timeout=1):
                    return address
            except OSError:
                pass
        time.sleep(1)
    raise AssertionError("chr-client has no management address")


def rcon(password: str, *commands: str) -> None:
    result = subprocess.run(
        ["python3", "-I", str(ROOT / "lab/chr/rcon.py"), "chr-lab", password, *commands],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise AssertionError(f"Router console command failed: {result.stdout}\n{result.stderr}")


def configure_router_tls(address: str, password: str) -> None:
    rcon(
        password,
        "/ip service set www-ssl disabled=yes certificate=none",
        '/certificate remove [find where name~"openpiso-e2e"]',
    )
    rcon(
        password,
        '/certificate add name="openpiso-e2e-ca" common-name="openpiso-e2e-ca" key-usage=key-cert-sign,crl-sign',
        "/certificate sign openpiso-e2e-ca",
    )
    rcon(
        password,
        f'/certificate add name="openpiso-e2e-server" common-name="{address}" subject-alt-name="IP:{address}" key-usage=tls-server',
        "/certificate sign openpiso-e2e-server ca=openpiso-e2e-ca",
    )
    rcon(
        password,
        "/ip service set www-ssl disabled=no certificate=openpiso-e2e-server tls-version=only-1.2",
    )

    context = ssl._create_unverified_context()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, 443), timeout=2) as raw:
                with context.wrap_socket(raw, server_hostname=address):
                    break
        except OSError:
            time.sleep(0.25)
    else:
        raise AssertionError("Router www-ssl did not become ready")

    existing = router_rest(address, "admin", password, f"/user?name={ROUTER_ADMIN}")
    for entry in existing:
        router_rest(address, "admin", password, f"/user/{entry['.id']}", "DELETE")
    router_rest(
        address,
        "admin",
        password,
        "/user",
        "PUT",
        {"name": ROUTER_ADMIN, "group": "full", "password": ROUTER_ADMIN_PASSWORD},
    )


def cleanup_router(password: str) -> None:
    rcon(password, f'/user remove [find where name="{ROUTER_ADMIN}"]')


def router_rest(address: str, username: str, password: str, path: str, method: str = "GET", body=None, scheme: str = "https"):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{scheme}://{address}/rest{path}", data=data, method=method)
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {credentials}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=10) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # RouterOS gives the reason in the body; for /tool/fetch it names the fetched URL's own
        # status. Keep it in the traceback, and re-raise with the body intact for callers.
        detail = error.read()
        failure = urllib.error.HTTPError(error.url, error.code, error.msg, error.hdrs, io.BytesIO(detail))
        failure.add_note(f"RouterOS {method} {path}: {detail.decode(errors='replace')[:500]}")
        raise failure from None


def device_internet(client_address: str, password: str, device_ip: str) -> tuple[str, str]:
    """Fetch from the Device for checks that expect internet; returns (target, body).

    1.1.1.1 drops HTTP some of the time on the lab network, so fall back to
    8.8.8.8 over HTTPS (plain HTTP there never answers). Checks that expect a
    redirect or a block must keep fetching 1.1.1.1 directly: a fallback could
    make a blocked Device look online.
    """
    try:
        target = "http://1.1.1.1/"
        result = router_rest(client_address, "admin", password, "/tool/fetch", "POST", {
            "url": target, "output": "user", "src-address": device_ip}, scheme="http")
    except OSError:
        target = "https://8.8.8.8/"
        result = router_rest(client_address, "admin", password, "/tool/fetch", "POST", {
            "url": target, "output": "user", "src-address": device_ip}, scheme="http")
    return target, result[-1].get("data", "")


def configure_device_routing(password: str) -> None:
    # libvirt installs the host return route without sudo. A temporary third
    # chr-lab NIC is its next hop; only the two throwaway CHR VMs are involved.
    # Never replace source identity with redirect parameters to work around NAT.
    xml = '''<network><name>openpiso-e2e-routing</name><bridge name="virbr-opiso7"/>
      <ip address="10.77.7.1" netmask="255.255.255.0"/>
      <route address="10.5.50.0" prefix="24" gateway="10.77.7.2"/>
      <route address="10.5.10.0" prefix="24" gateway="10.77.7.2"/>
      <route address="10.5.20.0" prefix="24" gateway="10.77.7.2"/>
    </network>'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml") as definition:
        definition.write(xml)
        definition.flush()
        # Right after an interrupted run libvirt can still be tearing the old
        # network down, so retry briefly and show virsh's own error.
        for attempt in range(10):
            created = subprocess.run(["virsh", "-c", "qemu:///system", "net-create", definition.name],
                                     capture_output=True, text=True)
            if created.returncode == 0:
                break
            time.sleep(3)
        else:
            raise AssertionError("virsh net-create failed: " + created.stderr.strip())
    subprocess.run(["virsh", "-c", "qemu:///system", "attach-interface", "chr-lab",
                    "network", "openpiso-e2e-routing", "--model", "virtio", "--mac", "52:54:00:07:07:07", "--live"],
                   check=True, capture_output=True)
    rcon(password,
         '/ip address add address=10.77.7.2/24 interface=[/interface ethernet find where mac-address="52:54:00:07:07:07"] comment="openpiso-e2e-routing"',
         f'/ip firewall nat add chain=srcnat src-address=10.5.0.0/16 dst-address={urllib.parse.urlsplit(PORTAL_URL).hostname} action=accept place-before=0 comment="openpiso-e2e-routing"')


# A live unplug needs the guest's acknowledgement, so re-send it until the NIC is gone;
# destroying the network under a lingering NIC breaks the next run's domifaddr.
def detach_device_routing() -> None:
    for _ in range(20):
        interfaces = subprocess.run(["virsh", "-c", "qemu:///system", "domiflist", "chr-lab"],
                                    check=True, capture_output=True, text=True, timeout=30).stdout
        if "52:54:00:07:07:07" not in interfaces:
            return
        subprocess.run(["virsh", "-c", "qemu:///system", "detach-interface", "chr-lab", "network",
                        "--mac", "52:54:00:07:07:07", "--live"], capture_output=True, timeout=30)
        time.sleep(3)
    raise RuntimeError("chr-lab kept the routing NIC after detach")


def destroy_device_network() -> None:
    networks = subprocess.run(["virsh", "-c", "qemu:///system", "net-list", "--name"],
                              check=True, capture_output=True, text=True, timeout=30).stdout.splitlines()
    if "openpiso-e2e-routing" in networks:
        subprocess.run(["virsh", "-c", "qemu:///system", "net-destroy", "openpiso-e2e-routing"],
                       check=True, capture_output=True, timeout=30)


def cleanup_hotspot_users(address, password, original_names) -> None:
    for user in router_rest(address, "admin", password, "/ip/hotspot/user"):
        if user["name"].startswith("op-") and user["name"] not in original_names:
            for active in router_rest(address, "admin", password, "/ip/hotspot/active?user=" + user["name"]):
                router_rest(address, "admin", password, "/ip/hotspot/active/" + active[".id"], "DELETE")
            router_rest(address, "admin", password, "/ip/hotspot/user/" + user[".id"], "DELETE")


def cleanup_steps(report, steps) -> None:
    # Attempt every resource even if a previous cleanup failed, and never replace
    # the scenario's original exception with a secondary teardown exception.
    original_error = sys.exception()
    failures = []
    for name, operation in steps:
        try:
            operation()
        except Exception as error:
            failures.append({"step": name, "error": type(error).__name__})
    report.data["cleanup"] = {"result": "fail" if failures else "pass", "failures": failures}
    if failures:
        message = "Cleanup failures: " + ", ".join(item["step"] + " (" + item["error"] + ")" for item in failures)
        if original_error is not None:
            original_error.add_note(message)
        else:
            raise AssertionError(message)


def device_fetch(client, device_ip: str, interface: str, table: str, url: str):
    # Probe with a GET before scenario mutations: RouterOS also wraps expected
    # portal errors in HTTP 400, so those requests must not be retried here.
    deadline = time.monotonic() + 30
    detail = "Device address or routing fixture not ready"
    while time.monotonic() < deadline:
        addresses = client(f"/ip/address?interface={interface}")
        leases = client(f"/ip/dhcp-client?interface={interface}")
        routes = client("/ip/route?comment=openpiso-e2e-device")
        rules = client("/routing/rule?comment=openpiso-e2e-device")
        ready = (
            any(entry["address"].split("/")[0] == device_ip and entry.get("invalid") != "true"
                and entry.get("disabled") != "true" for entry in addresses)
            # A lease mid-renewal ("renewing...") keeps its address; long ARM runs hit this.
            and any(entry.get("status", "").startswith(("bound", "renewing")) for entry in leases)
            and any(entry.get("routing-table") == table and entry.get("dst-address") == "0.0.0.0/0"
                    and entry.get("gateway") == f"10.5.50.1%{interface}" and entry.get("active") == "true"
                    for entry in routes)
            and any(entry.get("src-address") == f"{device_ip}/32" and entry.get("table") == table
                    and entry.get("action") == "lookup-only-in-table" and entry.get("disabled") != "true"
                    and entry.get("invalid") != "true" for entry in rules)
        )
        if not ready:
            detail = ("Device address or routing fixture not ready: addresses="
                      f"{[(e['address'], e.get('invalid'), e.get('disabled')) for e in addresses]} "
                      f"leases={[e.get('status') for e in leases]} "
                      f"routes={[(e.get('routing-table'), e.get('gateway'), e.get('active')) for e in routes]} "
                      f"rules={[(e.get('src-address'), e.get('table'), e.get('invalid')) for e in rules]}")
        if ready:
            try:
                return client("/tool/fetch", "POST", {"url": url, "output": "user", "src-address": device_ip})
            except urllib.error.HTTPError as error:
                if error.code != 400:
                    raise
                detail = f"HTTP 400: {error.read().decode(errors='replace')}"
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise AssertionError(f"Device fetch from {device_ip} on {interface} via {table} to {url} failed: {detail}")


def device_redirect(client_address: str, password: str) -> tuple[str, str]:
    def client(path: str, method: str = "GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")

    for name, disabled in (("lan1", "no"), ("lan2", "yes")):
        interfaces = client(f"/interface/ethernet?name={name}")
        client(f"/interface/ethernet/{interfaces[0]['.id']}", "PATCH", {"disabled": disabled})

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        addresses = client("/ip/address?interface=lan1")
        if addresses:
            break
        time.sleep(1)
    else:
        raise AssertionError("chr-client lan1 has no HotSpot address")

    routes = client("/ip/route?comment=openpiso-e2e-controller-route")
    for route in routes:
        client(f"/ip/route/{route['.id']}", "DELETE")
    client("/ping", "POST", {"address": "10.5.50.1", "count": "1"})

    device_ip = addresses[0]["address"].split("/")[0]
    tables = client("/routing/table?name=openpiso-e2e-device")
    if not tables:
        client("/routing/table", "PUT", {"name": "openpiso-e2e-device", "fib": "yes"})
    for route in client("/ip/route?comment=openpiso-e2e-device"):
        client(f"/ip/route/{route['.id']}", "DELETE")
    client("/ip/route", "PUT", {
        "dst-address": "0.0.0.0/0", "gateway": "10.5.50.1%lan1",
        "routing-table": "openpiso-e2e-device", "comment": "openpiso-e2e-device",
    })
    for rule in client("/routing/rule?comment=openpiso-e2e-device"):
        client(f"/routing/rule/{rule['.id']}", "DELETE")
    client("/routing/rule", "PUT", {
        "src-address": f"{device_ip}/32", "action": "lookup-only-in-table",
        "table": "openpiso-e2e-device", "comment": "openpiso-e2e-device",
    })
    redirect = device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", "http://1.1.1.1/")
    portal = device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", f"{PORTAL_URL}/portal")
    try:
        client("/tool/fetch", "POST", {"url": f"{PORTAL_URL}/admin/login", "output": "user", "src-address": device_ip})
    except urllib.error.HTTPError as error:
        detail = json.load(error).get("detail", "")
        assert "403" in detail, detail
    else:
        raise AssertionError("customer Device could reach the admin login")
    return redirect[-1].get("data", ""), portal[-1].get("data", "")


def cleanup_vendo_router(address: str, password: str):
    # Only disposable Controller-owned Vendo fixtures, including failed runs.
    for path in ("/ip/hotspot/ip-binding", "/ip/dhcp-server/lease", "/ip/firewall/filter", "/ipv6/firewall/filter"):
        for entry in router_rest(address, "admin", password, path):
            if entry.get("comment", "").startswith("openpiso-vendo-"):
                router_rest(address, "admin", password, f"{path}/{entry['.id']}", "DELETE")


def pair_board(opener, client_address, password, board):
    response = request(opener, "/admin/vendos/pair", "POST", {"mac": board["mac"]})
    assert response.status == 201, response.read().decode()
    paired = json.load(response)
    client = lambda path, method="GET", body=None: router_rest(client_address, "admin", password, path, method, body, scheme="http")
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    _flash_module.adopt(sys.modules[__name__], opener, client, board["ip"], vendo)
    return paired


def coin_board(client_address, password):
    client = lambda path, method="GET", body=None: router_rest(client_address, "admin", password, path, method, body, scheme="http")
    name = "opnw-coin-board"
    mac = _lab_module.COIN_BOARD_MAC
    if not client("/interface/macvlan?name=" + name):
        client("/interface/macvlan", "PUT", {"name": name, "interface": "lan1", "mac-address": mac})
        client("/ip/dhcp-client", "PUT", {"interface": name, "add-default-route": "no", "use-peer-dns": "no", "disabled": "no"})
        deadline = time.monotonic() + 30
        while not client("/ip/address?interface=" + name):
            assert time.monotonic() < deadline, "coin board DHCP did not bind"
            time.sleep(0.5)
        if not client("/routing/table?name=" + name):
            client("/routing/table", "PUT", {"name": name, "fib": "yes"})
        client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": "10.5.50.1%" + name, "routing-table": name, "comment": name})
    ip = client("/ip/address?interface=" + name)[0]["address"].split("/")[0]
    if not client("/routing/rule?comment=" + name):
        client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table", "table": name, "comment": name})
    return {"mac": mac, "ip": ip}


def cleanup_coin_board(client_address, password):
    client = lambda path, method="GET", body=None: router_rest(client_address, "admin", password, path, method, body, scheme="http")
    for path, query in (("/routing/rule", "comment=opnw-coin-board"), ("/ip/route", "comment=opnw-coin-board"),
                        ("/ip/dhcp-client", "interface=opnw-coin-board"), ("/interface/macvlan", "name=opnw-coin-board")):
        for row in client(path + "?" + query):
            client(path + "/" + row[".id"], "DELETE")


def vendo_pairing(opener, address: str, password: str, client_address: str, report):
    mac = _lab_module.DEVICE_MAC
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    paired = pair_board(opener, client_address, password, {"mac": mac, "ip": device_ip})
    assert len(bytes.fromhex(paired["secret"])) == 32
    vendo_id = paired["id"]
    comment = f"openpiso-vendo-{vendo_id}"
    lease = router_rest(address, "admin", password, f"/ip/dhcp-server/lease?comment={comment}")
    assert len(lease) == 1 and lease[0]["dynamic"] == "false"
    assert lease[0]["mac-address"].upper() == mac and lease[0]["address"] == device_ip
    bypass = router_rest(address, "admin", password, f"/ip/hotspot/ip-binding?comment={comment}")
    assert len(bypass) == 1 and bypass[0]["type"] == "bypassed"
    assert bypass[0]["mac-address"].upper() == mac and bypass[0]["address"] == device_ip
    assert request(opener, "/admin/vendos/pair", "POST", {"mac": mac}).status == 409
    state = json.load(request(opener, "/admin/state"))
    assert len(state["vendos"]) == 1
    assert "secret" not in state["vendos"][0]
    assert paired["secret"] not in json.dumps(state)
    assert state["vendos"][0]["last_contact"]

    # This Device already has the paired MAC: no Vendo key, no internet.
    def client(path, method="GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")

    def fetch(url):
        # Expected failures must not use device_fetch's readiness/retry loop.
        return client("/tool/fetch", "POST", {
            "url": url, "output": "user", "src-address": device_ip, "duration": "3s",
        })

    controller_rule = router_rest(address, "admin", password, "/ip/hotspot/walled-garden/ip?comment=openpiso-controller")[0]
    controller_url = f"http://{controller_rule['dst-address']}:{controller_rule['dst-port']}"
    portal = device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", controller_url + "/portal")
    assert "OpenPiso WiFi" in portal[-1].get("data", "")
    blocked_fetches = []
    for url in ("http://1.1.1.1/", f"http://{urllib.parse.urlsplit(PORTAL_URL).hostname}:18081/", "http://10.5.50.1/"):
        try:
            result = fetch(url)
        except urllib.error.HTTPError as error:
            blocked_fetches.append({"url": url, "router_error": json.load(error).get("detail", "")})
        else:
            # RouterOS duration-limited fetch can return only 'connecting'
            # progress without raising an error when no HTTP reply arrives.
            assert result and all(row.get("status") != "finished" and not row.get("data") for row in result), result
            blocked_fetches.append({"url": url, "progress": result})
    filters = router_rest(address, "admin", password, "/ip/firewall/filter")
    owned = [rule for rule in filters if rule.get("comment", "").startswith(comment + "-")]
    assert len(owned) == 5
    assert int(next(rule for rule in owned if rule["comment"] == comment + "-drop")["packets"]) > 0
    ipv6 = router_rest(address, "admin", password, "/ipv6/firewall/filter")
    ipv6_owned = [rule for rule in ipv6 if rule.get("comment", "").startswith(comment + "-")]
    assert {rule["chain"] for rule in ipv6_owned} == {"input", "forward"}
    assert all(rule["action"] == "drop" and rule["src-mac-address"].upper() == mac for rule in ipv6_owned)
    report.snapshot("vendo-paired-mac-spoof-restricted-to-controller", address, password, opener,
                    vendo_id=vendo_id, lease=lease, bypass=bypass, firewall=owned, ipv6_firewall=ipv6_owned,
                    blocked_fetches=blocked_fetches)
    return paired


def vendo_protocol(opener, paired, address: str, password: str, report):
    _vendo_module.assert_firmware_vectors()
    response = request(opener, "/admin/vendos/pair", "POST", {"mac": _lab_module.UNKNOWN_VENDO_MAC})
    assert response.status == 201, response.read().decode()
    second = json.load(response)
    assert second["secret"] != paired["secret"]
    wrong_key = _vendo_module.sign(1, (second["id"], 1, 99, 0, 1, 1), bytes.fromhex(paired["secret"]))
    attacker = SimulatedVendo(BASE_URL, second["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    assert attacker.post(wrong_key)[0] == 401
    assert request(opener, "/admin/vendos/unpair", "POST", {"id": second["id"]}).status == 204
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), epoch=2, timeout=HTTP_TIMEOUT)
    vendo.nonce = 0xA1B2C3D4
    vendo.rseq = 8
    vendo.total = 300
    vendo.window_id = 7  # #16 has not issued this window: audit, never a balance.
    first = vendo.frame()
    status, body = vendo.post(first)
    assert status == 200
    expected_ack = _vendo_module.sign(3, (paired["id"], 2, 300, 0xA1B2C3D4, 9), vendo.key)
    assert body == expected_ack

    def protocol_state():
        return json.load(request(opener, "/admin/state"))["vendo_protocol"]

    state = protocol_state()
    assert [row["coins"] for row in state["coin_deltas"]] == [300]
    assert next(row for row in state["totals"] if row["epoch"] == 2)["last_seen"] == 300
    assert any(row["reason"] == "unassigned_window" and row["coins"] == 300 for row in state["audit"])
    assert vendo.post(first)[1] == expected_ack  # exact replay
    vendo.total = 305
    lost_ack = vendo.frame()
    assert vendo.post(lost_ack, ignore_ack=True)[0] == 200
    after_lost_ack = protocol_state()
    assert [row["coins"] for row in after_lost_ack["coin_deltas"]] == [300, 5]
    assert vendo.post(lost_ack)[0] == 200
    assert vendo.post(first)[1] == expected_ack  # reordered older total and freshness
    assert protocol_state()["coin_deltas"] == after_lost_ack["coin_deltas"]
    newest = json.load(request(opener, "/admin/state"))["vendos"][0]
    assert newest["boot_nonce"] == 0xA1B2C3D4 and newest["rseq"] == 10
    assert newest["last_contact"]

    # Freshness must not become a dedup key: same rseq, larger durable total.
    same_seq = _vendo_module.sign(1, (vendo.id, 2, 307, 7, vendo.nonce, 10), vendo.key)
    assert vendo.post(same_seq)[0] == 200
    assert [row["coins"] for row in protocol_state()["coin_deltas"]] == [300, 5, 2]
    # A new boot nonce replaces the command echo, even with a smaller rseq.
    vendo.nonce = 123
    vendo.rseq = 0
    assert vendo.post(vendo.frame())[0] == 200  # older total still credits zero
    newest = json.load(request(opener, "/admin/state"))["vendos"][0]
    assert (newest["boot_nonce"], newest["rseq"]) == (123, 1)

    before_forgery = protocol_state()
    last_contact = newest["last_contact"]
    forged = bytearray(vendo.frame())
    forged[-1] ^= 1
    invalid_frames = [
        bytes(forged), bytes(first[:25]), b"", first + b"extra", b"\xff" + first[1:],
        _vendo_module.sign(1, (0xFFFFFFFE, 2, 900, 7, 123, 99), vendo.key),
        _vendo_module.sign(2, (vendo.id, 7, 45000, 123, 99), vendo.key),
    ]
    for frame in invalid_frames:
        assert vendo.post(frame)[0] in (400, 401, 404)
    for path in ("/vendo/messages", "/vendo/messages?report=" + first.hex()):
        assert request(opener, path).status == 405
    state = protocol_state()
    assert state["coin_deltas"] == before_forgery["coin_deltas"]
    assert state["totals"] == before_forgery["totals"]
    assert json.load(request(opener, "/admin/state"))["vendos"][0]["last_contact"] == last_contact
    assert {row["reason"] for row in state["audit"]} >= {"bad_hmac", "unknown_vendo", "wrong_type", "stale_total", "stale_freshness"}

    # Each epoch retains its own high water mark, even when an old epoch returns.
    vendo.epoch = 3
    vendo.total = 4
    assert vendo.post(vendo.frame(5))[0] == 200  # closed has report's layout/accounting
    assert vendo.post(vendo.frame(5))[0] == 200
    vendo.epoch = 2
    vendo.total = 307
    assert vendo.post(vendo.frame())[0] == 200
    assert [row["coins"] for row in protocol_state()["coin_deltas"]] == [300, 5, 2, 4]

    # Signed 32-bit serial arithmetic across a total wrap; literals are worked
    # deltas, not an implementation-derived expected value.
    vendo.epoch = 4
    for total in (0x7FFFFFF0, 0xFFFFFFE0, 16):
        vendo.total = total
        assert vendo.post(vendo.frame())[0] == 200
    assert [row["coins"] for row in protocol_state()["coin_deltas"]][-3:] == [2147483632, 2147483632, 48]
    # Worked deltas: 300 + 5 + 2 + 4 + 2147483632 + 2147483632 + 48.
    assert sales(opener)["coin_pesos"] == 4294967623, "replay or audit copies inflated coin Sales"
    report.snapshot("vendo-authenticated-cumulative-reports-and-wire-vectors", address, password, opener,
                    firmware_vectors="report/open/ack", close_closed_layouts="issue-15; firmware vectors pending #21",
                    duplicated_and_dropped_ack_extra_coins=0, forged_extra_coins=0,
                    unknown_window_credits_no_time=True, total_wrap_deltas=[2147483632, 2147483632, 48])
    return vendo


def vendo_unpair(opener, paired, address: str, password: str, report):
    response = request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]})
    assert response.status == 204, response.read().decode()
    comment = f"openpiso-vendo-{paired['id']}"
    for path in ("/ip/dhcp-server/lease", "/ip/hotspot/ip-binding", "/ip/firewall/filter", "/ipv6/firewall/filter"):
        entries = router_rest(address, "admin", password, path)
        assert not any(entry.get("comment", "").startswith(comment) for entry in entries)
    state = json.load(request(opener, "/admin/state"))
    assert state["vendos"][0]["status"] == "unpaired"
    report.snapshot("vendo-unpaired-router-rules-removed", address, password, opener)


def renew_device_lease(client_address: str, password: str, *, verify_portal: bool = True) -> None:
    """Give chr-client lan1 a fresh HotSpot lease after its paired (static) lease was deleted.

    Unicast renewals are refused by the HotSpot, so the next broadcast rebind for
    a lease the server no longer has gets a new address minutes later, mid-scenario.
    The new lease may differ, so the Device routing rule follows it. Run-start
    cleanup skips the Portal probe because its routing fixture does not exist yet.
    """
    client = lambda path, method="GET", body=None: router_rest(client_address, "admin", password, path, method, body, scheme="http")
    lease = client("/ip/dhcp-client?interface=lan1")[0]
    client("/ip/dhcp-client/release", "POST", {".id": lease[".id"]})
    deadline = time.monotonic() + 30
    while (lease := client("/ip/dhcp-client?interface=lan1")[0]).get("status") != "bound":
        assert time.monotonic() < deadline, "chr-client lan1 did not get a new lease"
        time.sleep(0.5)
    device_ip = lease["address"].split("/")[0]
    for rule in client("/routing/rule?comment=openpiso-e2e-device"):
        client(f"/routing/rule/{rule['.id']}", "PATCH", {"src-address": f"{device_ip}/32"})
    if verify_portal:
        device_fetch(client, device_ip, "lan1", "openpiso-e2e-device", f"{PORTAL_URL}/portal")


def capture_baseline(address: str, password: str) -> dict:
    groups = router_rest(address, "admin", password, "/user/group?name=openpiso-controller")
    users = router_rest(address, "admin", password, "/user?name=openpiso-controller")
    profiles = router_rest(address, "admin", password, "/ip/hotspot/user/profile")
    hotspot_profiles = router_rest(address, "admin", password, "/ip/hotspot/profile")
    walled = router_rest(address, "admin", password, "/ip/hotspot/walled-garden/ip")
    files = router_rest(address, "admin", password, "/file?name=openpiso%2Flogin.html")
    return {
        "group": [{key: item.get(key, "") for key in ("name", "policy")} for item in groups],
        "user": [{key: item.get(key, "") for key in ("name", "group", "disabled")} for item in users],
        "profiles": sorted(
            [
                {key: item.get(key, "") for key in ("name", "idle-timeout", "keepalive-timeout", "shared-users", "add-mac-cookie")}
                for item in profiles
                if item["name"].startswith("openpiso-")
            ],
            key=lambda item: item["name"],
        ),
        "hotspot_profiles": sorted(
            [{key: item.get(key, "") for key in ("name", "login-by", "html-directory")} for item in hotspot_profiles],
            key=lambda item: item["name"],
        ),
        "walled_garden": [
            {key: item.get(key, "") for key in ("comment", "action", "dst-address", "dst-port", "protocol")}
            for item in walled
            if item.get("comment") == "openpiso-controller"
        ],
        # The Controller's login page exists once a HotSpot server is Adopted.
        "login_html": files[0]["contents"] if files else None,
        "login_file": {key: files[0].get(key, "") for key in ("name", "size", "last-modified")} if files else None,
    }


def wait_for(url: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise AssertionError(f"Controller did not become ready at {url}")


class Controller:
    def __init__(self, binary: str, data_dir: str, initialize: bool):
        if ARM_VM is not None:
            ARM_VM.start_controller()
            wait_for(f"{BASE_URL}/healthz", timeout=120)
            return
        env = os.environ.copy()
        if initialize:
            env["OPENPISO_ADMIN_PASSWORD"] = ADMIN_PASSWORD
        else:
            env.pop("OPENPISO_ADMIN_PASSWORD", None)
        self.process = subprocess.Popen(
            [
                binary,
                "-listen",
                "0.0.0.0:18080",
                "-data",
                data_dir,
                "-portal-url",
                PORTAL_URL,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for(f"{BASE_URL}/healthz")

    def kill(self) -> None:
        # Abrupt crash for recovery scenarios.
        if ARM_VM is not None:
            ARM_VM.kill_controller()
            return
        self.process.kill()
        self.process.wait(timeout=5)

    def stop(self) -> None:
        if ARM_VM is not None:
            ARM_VM.stop_controller()
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        if self.process.returncode not in (0, -15, -9):
            output = self.process.stdout.read() if self.process.stdout else ""
            raise AssertionError(f"Controller exited {self.process.returncode}:\n{output}")


def request(opener, path: str, method: str = "GET", form=None, headers=None, data=None):
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        return opener.open(req, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as error:
        return error


def assert_login_redirect(path: str) -> None:
    """Anonymous full-page GETs follow the 303 to login, preserving their exact URL."""
    response = request(urllib.request.build_opener(), path)
    target = urllib.parse.urlsplit(response.url)
    assert response.status == 200 and target.path == "/admin/login"
    assert urllib.parse.parse_qs(target.query)["next"] == [path]


def sales(opener, start="1970-01-01", end="2100-01-01"):
    """The /admin/sales JSON for inclusive local days start..end (all time by default)."""
    query = urllib.parse.urlencode({"format": "json", "from": start, "to": end})
    return json.load(request(opener, "/admin/sales?" + query))


def non_v7_refusal(opener, report, address, password):
    spec = importlib.util.spec_from_file_location("non_v7_router", Path(__file__).with_name("non_v7_router.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = json.load(request(opener, "/admin/state"))
    with module.NonV7Router() as router:
        assert request(opener, "/admin/router/probe", "POST", {
            "address": router.address, "username": "fixture", "password": "not-a-router-secret"}).status == 200
        response = request(opener, "/admin/router/confirm", "POST")
        message = response.read().decode()
        assert response.status == 502 and "RouterOS v7 is required" in message and "6.49.18" in message
        assert router.requests == [("GET", "/rest/system/resource")], "refusal wrote to the non-v7 Router"
    after = json.load(request(opener, "/admin/state"))
    for key in before.keys() - {"reconciliation"}:
        assert after[key] == before[key], "refusal changed Controller state: " + key
    report.snapshot("non-v7-onboarding-refused-before-any-write", address, password, opener,
                    refusal_message=message.strip(), controller_unchanged=True, router_writes=0)


def validity_settings(opener, report, address, password):
    state = json.load(request(opener, "/admin/state"))
    assert state["validity_seconds"] == 2592000 and "validity_days" not in state
    page = request(opener, "/admin/validity").read().decode()
    assert 'name="value"' in page and 'value="30"' in page
    assert '<option value="days" selected>' in page
    assert "Unused time expires 30 days after the last Top-up." in page
    for value, unit in (("0", "seconds"), ("-1", "days"), ("NaN", "seconds"),
                        ("Inf", "days"), ("36501", "days"), ("no", "days"),
                        ("1.5", "days"), ("9223372036854775807", "days"), ("1", "weeks")):
        assert request(opener, "/admin/validity", "POST", {"value": value, "unit": unit}).status == 400
    for value, unit, seconds, displayed in (("3", "seconds", 3, "3 seconds"),
                                          ("120", "seconds", 120, "2 minutes"),
                                          ("60", "minutes", 3600, "1 hour"),
                                          ("24", "hours", 86400, "1 day")):
        response = request(opener, "/admin/validity", "POST", {"value": value, "unit": unit})
        assert response.status == 200
        page = response.read().decode()
        assert f"Unused time expires {displayed} after the last Top-up." in page
        assert json.load(request(opener, "/admin/state"))["validity_seconds"] == seconds
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200
    report.snapshot("validity-whole-number-units-and-readable-preview", address, password, opener,
                    default_seconds=2592000, short_validity_seconds=3, invalid_settings_rejected=True)


def generate_vouchers(opener, form):
    return request(opener, "/admin/vouchers", "POST", {"tier": "custom", "pesos": "0", **form})


def voucher_print_url(response):
    batch = urllib.parse.parse_qs(urllib.parse.urlsplit(response.url).query)["generated"][0]
    return "/admin/vouchers?batch=" + urllib.parse.quote(batch)


def voucher_csv(opener, response):
    path = voucher_print_url(response).replace("/admin/vouchers?", "/admin/vouchers/export?")
    download = request(opener, path)
    assert download.status == 200 and "attachment" in download.headers["Content-Disposition"]
    reader = csv.DictReader(io.StringIO(download.read().decode()))
    assert reader.fieldnames == ["code", "time", "pesos", "redeemed_at"]
    return list(reader)


def voucher_codes(opener, response):
    return [row["code"] for row in voucher_csv(opener, response)]


def vouchers(opener, report, address, password, client_address):
    """Voucher feature entry point; later tickets add one sub-function per behaviour."""
    device_redirect(client_address, password)
    saved = json.load(request(opener, "/admin/state"))["rate_tiers"]
    milli = lambda m: f"{m // 1000}.{m % 1000:03d}".rstrip("0").rstrip(".")
    saved_rates = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]},{milli(t.get("points_milli", 0))}' for t in saved)
    try:
        tier = vouchers_batch_pricing(opener, report, address, password)
        vouchers_tier_redemption(opener, report, address, password, client_address, tier)
        vouchers_print_sheet(opener, report, address, password, tier)
        vouchers_template_editor(opener, report, address, password, tier)
        vouchers_code_formats(opener, report, address, password, client_address)
        vouchers_seller_sales(opener, report, address, password, client_address)
        vouchers_code_search(opener, report, address, password, client_address)
        vouchers_search_limit(opener, report, address, password)
        _browser_module.vouchers_mobile(sys.modules[__name__], report)
        vouchers_delete_unused(opener, report, address, password, client_address)
        vouchers_delete_redeemed_refused(opener, report, address, password, client_address)
    finally:
        assert request(opener, "/admin/rates", "POST", {"tiers": saved_rates}).status == 200


def vouchers_batch_pricing(opener, report, address, password):
    """#112: tier and Custom zero-price batches, numbering, list and CSV."""
    before = json.load(request(opener, "/admin/state"))
    assert_login_redirect("/admin/vouchers")
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=120,7200,2\n5=600,10800,7"}).status == 200
    page = request(opener, "/admin/vouchers").read().decode()
    assert "3 hours Validity" in page and "7 Pauses" in page
    tier = generate_vouchers(opener, {"tier": "5", "count": "2"})
    tier_rows = voucher_csv(opener, tier)
    assert len(tier_rows) == 2 and all(r["time"] == "600" and r["pesos"] == "5" and not r["redeemed_at"] for r in tier_rows)
    assert_login_redirect(voucher_print_url(tier))
    assert_login_redirect(voucher_print_url(tier).replace("/admin/vouchers?", "/admin/vouchers/export?"))
    promo = generate_vouchers(opener, {"seconds": "60", "pesos": "0", "count": "1"})
    assert voucher_csv(opener, promo)[0]["pesos"] == "0"
    refused = request(opener, "/admin/vouchers", "POST", {"tier": "custom", "seconds": "73", "count": "3", "pesos": ""})
    assert "error=" in refused.url
    refused_page = refused.read().decode()
    assert 'data-reopen="true"' in refused_page and 'name="seconds"' in refused_page and 'value="73"' in refused_page
    assert 'name="count"' in refused_page and 'value="3"' in refused_page and "Custom price is required" in refused_page
    state = json.load(request(opener, "/admin/state"))
    batches = state["purchases"]["batches"]
    tier_batch = next(b for b in batches if b["id"] in tier.url)
    promo_batch = next(b for b in batches if b["id"] in promo.url)
    assert promo_batch["number"] == tier_batch["number"] + 1
    assert promo_batch["validity_seconds"] == before["validity_seconds"]
    assert promo_batch["pause_limit"] == before["pause_settings"]["pause_limit"]
    page = request(opener, "/admin/vouchers").read().decode()
    assert page.index('data-batch="' + promo_batch["id"]) < page.index('data-batch="' + tier_batch["id"])
    assert "Shop" in page and "Custom" in page and "₱0" in page and 'target="_blank"' in page
    report.snapshot("vouchers-tier-and-custom-zero-batches-csv-and-list", address, password, opener,
                    tier_batch=tier_batch, promo_batch=promo_batch, tier_csv=tier_rows)
    return tier


def vouchers_tier_redemption(opener, report, address, password, client_address, tier):
    """#112: editing a tier leaves the batch terms and redeemed credit intact."""
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=30,60,1\n5=150,60,1"}).status == 200
    printable = request(opener, voucher_print_url(tier)).read().decode()
    data = json.loads(re.search(r'<script[^>]*data-voucher-batch[^>]*>(.*?)</script>', printable, re.S).group(1))
    assert data["batch"]["time"] == "10 minutes" and data["batch"]["validity"] == "3 hours"
    assert data["batch"]["pause-limit"] == "7" and data["batch"]["pesos"] == "₱5"
    code = voucher_codes(opener, tier)[0]
    started = time.time()
    portal = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code.lower()}))
    tc = portal["time_credit"]
    assert tc["bought"] == 600 and tc["pause_limit"] == tc["pauses_left"] == 7
    assert started + 10800 - 2 <= tc["valid_until"] <= time.time() + 10800 + 2
    state = json.load(request(opener, "/admin/state"))
    credit = time_credit_for_device(state["purchases"], _lab_module.DEVICE_MAC, device_ip)
    assert credit["points_milli"] == 0
    user = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + credit["id"])[0]
    assert duration_seconds(user["limit-uptime"]) == 600
    assert voucher_csv(opener, tier)[0]["redeemed_at"]
    report.snapshot("vouchers-tier-snapshot-redemption-and-router-limits", address, password, opener,
                    portal=portal, hotspot_user=user, redeemed_csv=voucher_csv(opener, tier))


def vouchers_print_sheet(opener, report, address, password, batch):
    """#117: browser rendering, nonce CSP and a redeemed card on reprint."""
    rows = voucher_csv(opener, batch)
    assert any(row["redeemed_at"] for row in rows)
    response = request(opener, voucher_print_url(batch))
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'nonce-" in csp and "'self'" not in csp
    for directive in ("default-src 'none'", "style-src 'unsafe-inline'", "img-src data:",
                      "connect-src 'none'", "form-action 'none'", "frame-ancestors 'none'"):
        assert directive in csp, csp
    again = request(opener, voucher_print_url(batch))
    assert again.headers["Content-Security-Policy"] != csp
    browser = _browser_module.voucher_print_sheet(sys.modules[__name__], report, voucher_print_url(batch), rows)
    report.snapshot("vouchers-print-sheet-rendered-and-used", address, password, opener, browser=browser, csp=csp)


def vouchers_template_editor(opener, report, address, password, batch):
    """#118: publish through the Settings editor, never intercept Print responses."""
    browser = _browser_module.voucher_template_editor(
        sys.modules[__name__], report, voucher_print_url(batch), voucher_csv(opener, batch))
    report.snapshot("vouchers-template-editor-publish-revert-reset", address, password, opener, browser=browser)


def vouchers_code_formats(opener, report, address, password, client_address):
    """#113: all formats and lengths, refusal, and case-insensitive prefixed redeem."""
    presets = [("digits", r"[0-9]"), ("uppercase", r"[A-Z]"),
               ("unambiguous", r"[A-HJ-NP-Z2-9]"), ("mixed", r"[A-Za-z0-9]")]
    default = generate_vouchers(opener, {"seconds": "60", "count": "1"})
    default_codes = voucher_codes(opener, default)
    assert len(default_codes) == 1 and re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", default_codes[0])
    generated = [{"preset": "default", "length": 8, "codes": default_codes}]
    seen = {c.lower() for c in default_codes}
    for length in range(4, 11):
        preset, pattern = presets[(length - 4) % len(presets)]
        response = generate_vouchers(opener, {"seconds": "60", "count": "2", "preset": preset, "length": str(length)})
        codes = voucher_codes(opener, response)
        assert len(codes) == 2 and all(re.fullmatch(pattern + "{" + str(length) + "}", c) for c in codes)
        assert all(c.lower() not in seen for c in codes) and len({c.lower() for c in codes}) == 2
        seen.update(c.lower() for c in codes)
        generated.append({"preset": preset, "length": length, "codes": codes})
    refused = generate_vouchers(opener, {"seconds": "60", "count": "101", "preset": "digits", "length": "4", "prefix": "Keep"})
    assert "error=" in refused.url
    page = refused.read().decode()
    assert "exceeds 1%" in page and 'data-reopen="true"' in page and 'value="Keep"' in page and 'value="101"' in page
    mixed = generate_vouchers(opener, {"seconds": "60", "count": "2", "preset": "mixed", "length": "10", "prefix": "Ab12Cd"})
    rows = voucher_csv(opener, mixed)
    assert all(re.fullmatch(r"Ab12Cd[A-Za-z0-9]{10}", r["code"]) for r in rows)
    state = json.load(request(opener, "/admin/state"))
    batch = next(b for b in state["purchases"]["batches"] if b["id"] in mixed.url)
    assert (batch["code_preset"], batch["code_length"], batch["code_prefix"]) == ("mixed", 10, "Ab12Cd")
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    before = time_credit_for_device(state["purchases"], _lab_module.DEVICE_MAC, device_ip)
    typed = rows[0]["code"].swapcase()
    assert typed != rows[0]["code"]
    portal = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": typed}))
    assert portal["time_credit"]["bought"] == before["bought_seconds"] + 60
    redeemed = voucher_csv(opener, mixed)
    assert next(r for r in redeemed if r["code"] == rows[0]["code"])["redeemed_at"]
    device_redeem_refused(client_address, password, device_ip, rows[0]["code"], 409)
    device_redeem_refused(client_address, password, device_ip, "bad!", 400)
    report.snapshot("vouchers-code-formats-and-prefixed-different-case-redemption", address, password, opener,
                    formats=generated, batch=batch, typed_code=typed, redeemed_csv=redeemed, portal=portal)


def vouchers_seller_sales(opener, report, address, password, client_address):
    """#114: Seller labels follow redeemed Vouchers into Sales and filtered CSV."""
    seller = "Aling Nena & Co"
    generated = generate_vouchers(opener, {"seconds": "60", "pesos": "7", "count": "1", "seller": "  " + seller + "  "})
    code = voucher_codes(opener, generated)[0]
    page = request(opener, "/admin/vouchers").read().decode()
    assert html.escape(seller) in page and 'name="seller"' in page
    assert '<option value="' + html.escape(seller) + '">' in page
    refused = generate_vouchers(opener, {"seconds": "60", "pesos": "", "count": "1", "seller": seller})
    assert 'value="' + html.escape(seller) + '"' in refused.read().decode()
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}))
    query = urllib.parse.urlencode({"from": "1970-01-01", "to": "2100-01-01", "seller": seller})
    filtered = json.load(request(opener, "/admin/sales?format=json&" + query))
    assert filtered["rows"] and all(r["type"] == "voucher" and r["seller"] == seller for r in filtered["rows"])
    assert any(r["user"] == code and r["amount"] == 7 for r in filtered["rows"])
    assert filtered["coin_pesos"] == 0 and filtered["voucher_pesos"] == 7
    for view in ("table", "feed"):
        document = request(opener, "/admin/sales?view=" + view + "&" + query).read().decode()
        assert html.escape(seller) in document and "Seller" in document
    download = request(opener, "/admin/sales/export?" + query).read().decode("utf-8-sig")
    exported = list(csv.DictReader(io.StringIO(download)))
    assert exported and all(r["seller"] == seller for r in exported)
    assert any(r["User"] == code for r in exported)
    promo = generate_vouchers(opener, {"seconds": "60", "pesos": "0", "count": "1", "seller": "   "})
    promo_code = voucher_codes(opener, promo)[0]
    portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": promo_code}))
    shop = json.load(request(opener, "/admin/sales?format=json&from=1970-01-01&to=2100-01-01&seller=Shop&page_size=100"))
    assert shop["rows"] and all(r["type"] == "voucher" and r["seller"] == "Shop" for r in shop["rows"])
    assert any(r["user"] == promo_code and r["amount"] == 0 for r in shop["rows"])
    assert not any(r["user"] == code for r in shop["rows"])
    missing = json.load(request(opener, "/admin/sales?format=json&seller=unused-seller-label"))
    assert missing["count"] == 0 and missing["total_pesos"] == 0
    unfiltered = sales(opener)
    assert all(r["seller"] == "" for r in unfiltered["rows"] if r["type"] == "coin")
    report.snapshot("vouchers-seller-sales-filter-and-csv", address, password, opener,
                    seller_sales=filtered, seller_csv=exported, shop_sales=shop)


def vouchers_code_search(opener, report, address, password, client_address):
    """#115: literal code search shows redemption details and opens the batch."""
    seller = "Search Seller & Co"
    generated = generate_vouchers(opener, {"seconds": "73", "pesos": "7", "count": "1", "seller": seller, "prefix": "FiNd"})
    code = voucher_codes(opener, generated)[0]
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}))
    # Exclude both ends: matching a prefix alone would not satisfy substring search.
    query = code[1:-1].lower()
    path = "/admin/vouchers?" + urllib.parse.urlencode({"q": query})
    document = request(opener, path).read().decode()
    results = document.split('id="voucher-search-results"', 1)[1].split("All Voucher Batches", 1)[0]
    assert "1 Voucher contains" in results and code in results
    assert html.escape(seller) in results and "₱7" in results and "1 minute 13 seconds" in results
    assert "Redeemed" in results and device_ip in results and _lab_module.DEVICE_MAC in results
    assert voucher_csv(opener, generated)[0]["redeemed_at"] in results
    assert voucher_print_url(generated) in results and 'target="_blank"' in results and 'hx-boost="false"' in results
    assert code in request(opener, voucher_print_url(generated)).read().decode()
    assert 'href="/admin/vouchers"' in results and "Clear search" in results
    cleared = request(opener, "/admin/vouchers").read().decode()
    assert 'id="voucher-search-results"' not in cleared
    for short in ("a", "ab"):
        page = request(opener, "/admin/vouchers?q=" + short).read().decode()
        assert "Type at least 3 characters" in page and 'class="voucher-search-row"' not in page
    for missing in ("not-a-voucher", "%__"):
        page = request(opener, "/admin/vouchers?" + urllib.parse.urlencode({"q": missing})).read().decode()
        assert "No Voucher code contains" in page and "Clear search" in page
        assert 'class="voucher-search-row"' not in page
    report.snapshot("vouchers-code-search-redeemed-device-and-print-sheet", address, password, opener,
                    query=query, code=code, seller=seller, redeemed_mac=_lab_module.DEVICE_MAC,
                    redeemed_ip=device_ip, print_url=voucher_print_url(generated), results_html=results)


def vouchers_search_limit(opener, report, address, password):
    """#115: 50 matches fit; overflow stays capped with newer batches first."""
    form = {"seconds": "60", "count": "49", "prefix": "Q115"}
    older = generate_vouchers(opener, form)
    newer = generate_vouchers(opener, {**form, "count": "1"})
    newest_code = voucher_codes(opener, newer)[0]
    path = "/admin/vouchers?q=q115"
    document = request(opener, path).read().decode()
    results = document.split('id="voucher-search-results"', 1)[1].split("All Voucher Batches", 1)[0]
    codes = re.findall(r'<code>(Q115[^<]+)</code>', results)
    assert len(codes) == 50 and codes[0] == newest_code
    assert set(codes) == set(voucher_codes(opener, older) + [newest_code])
    assert "50 Vouchers contain" in results and "More Vouchers match" not in results
    assert "Shop" in results and "Unused" in results
    latest = generate_vouchers(opener, {**form, "count": "2"})
    latest_codes = sorted(voucher_codes(opener, latest))
    document = request(opener, path).read().decode()
    results = document.split('id="voucher-search-results"', 1)[1].split("All Voucher Batches", 1)[0]
    codes = re.findall(r'<code>(Q115[^<]+)</code>', results)
    assert len(codes) == 50 and codes[:2] == latest_codes and codes[2] == newest_code
    assert "More Vouchers match" in results and "type more characters" in results
    assert "All Voucher Batches" in document
    report.snapshot("vouchers-search-cap-and-newest-batch-first", address, password, opener,
                    returned_codes=codes, total_matches=52, results_html=results)


def vouchers_delete_unused(opener, report, address, password, client_address):
    """#116: deletion removes unused Vouchers and leaves exactly one audit line."""
    generated = generate_vouchers(opener, {"seconds": "60", "count": "2", "seller": "Delete Seller & Co"})
    codes = voucher_codes(opener, generated)
    state = json.load(request(opener, "/admin/state"))
    batch = next(b for b in state["purchases"]["batches"] if b["id"] in generated.url)
    path = "/admin/vouchers/delete?" + urllib.parse.urlencode({"batch": batch["id"]})
    page = request(opener, "/admin/vouchers").read().decode()
    row = page.split('data-batch="' + batch["id"] + '"', 1)[1].split("</tr>", 1)[0]
    assert "Delete" in row and "2 Vouchers will stop working" in row
    anonymous = request(urllib.request.build_opener(), path, "POST", {})
    assert anonymous.status == 401
    assert request(opener, path).status == 405
    assert request(opener, path, "POST", {}).status == 200
    assert request(opener, voucher_print_url(generated)).status == 404
    assert request(opener, voucher_print_url(generated).replace("/admin/vouchers?", "/admin/vouchers/export?")).status == 404
    assert 'data-batch="' + batch["id"] + '"' not in request(opener, "/admin/vouchers").read().decode()
    assert all(b["id"] != batch["id"] for b in json.load(request(opener, "/admin/state"))["purchases"]["batches"])
    audit_line = f'Batch #{batch["number"]}: 2 Vouchers · Seller: Delete Seller &amp; Co · admin'
    logs = request(opener, "/admin/logs?kind=audit").read().decode()
    assert logs.count(audit_line) == 1 and "Voucher Batch deleted" in logs
    assert request(opener, path, "POST", {}).status == 404
    assert request(opener, "/admin/logs?kind=audit").read().decode().count(audit_line) == 1
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    device_redeem_refused(client_address, password, device_ip, codes[0], 400)
    next_batch = generate_vouchers(opener, {"seconds": "60", "count": "1"})
    next_state = json.load(request(opener, "/admin/state"))
    assert next(b for b in next_state["purchases"]["batches"] if b["id"] in next_batch.url)["number"] > batch["number"]
    report.snapshot("vouchers-delete-unused-and-append-only-audit", address, password, opener,
                    deleted_batch=batch, deleted_codes=codes, audit_line=audit_line, logs_html=logs)


def vouchers_delete_redeemed_refused(opener, report, address, password, client_address):
    """#116: a direct POST cannot delete a batch with one redeemed Voucher."""
    generated = generate_vouchers(opener, {"seconds": "60", "count": "2"})
    codes = voucher_codes(opener, generated)
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[0]}))
    state = json.load(request(opener, "/admin/state"))
    batch = next(b for b in state["purchases"]["batches"] if b["id"] in generated.url)
    before_csv = voucher_csv(opener, generated)
    before_logs = request(opener, "/admin/logs?kind=audit").read().decode()
    page = request(opener, "/admin/vouchers").read().decode()
    row = page.split('data-batch="' + batch["id"] + '"', 1)[1].split("</tr>", 1)[0]
    assert "Delete" not in row
    response = request(opener, "/admin/vouchers/delete?" + urllib.parse.urlencode({"batch": batch["id"]}), "POST", {})
    assert response.status == 409 and "redeemed Voucher" in response.read().decode()
    assert voucher_csv(opener, generated) == before_csv
    assert request(opener, voucher_print_url(generated)).status == 200
    after_logs = request(opener, "/admin/logs?kind=audit").read().decode()
    assert re.findall(r'id="ev-[^"]+"', after_logs) == re.findall(r'id="ev-[^"]+"', before_logs)
    assert f'Batch #{batch["number"]}:' not in after_logs
    after = json.load(request(opener, "/admin/state"))
    assert after["purchases"] == state["purchases"]
    report.snapshot("vouchers-delete-redeemed-direct-post-refused", address, password, opener,
                    retained_batch=batch, retained_csv=before_csv, refusal_status=response.status)


def voucher_batch(opener, report, address, password):
    for form in ({"seconds": "0", "count": "1"}, {"seconds": "60", "count": "0"},
                 {"seconds": "60", "count": "1001"}):
        refused = generate_vouchers(opener, form)
        assert "error=" in refused.url and 'data-reopen="true"' in refused.read().decode()
    response = generate_vouchers(opener, {"seconds": "120", "count": "4"})
    assert response.status == 200
    printable = request(opener, voucher_print_url(response)).read().decode()
    codes = voucher_codes(opener, response)
    assert len(codes) == len(set(codes)) == 4, printable
    assert "print" in printable.lower()
    state = json.load(request(opener, "/admin/state"))
    assert len(state["purchases"]["batches"]) == 1
    assert state["purchases"]["batches"][0]["seconds"] == 120
    assert len(state["purchases"]["vouchers"]) == 4
    assert not state["purchases"]["time_credits"]
    report.snapshot("voucher-batch-printable-and-validated", address, password, opener,
                    generated_count=4, seconds=120, alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    return codes


def voucher_redemption(opener, report, address, password, client_address, codes):
    def client(path, method="GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")

    device_ip = client("/ip/address?interface=lan1")[0]["address"].split("/")[0]

    def fetch(path, code=None):
        body = {"url": PORTAL_URL + path, "output": "user",
                "src-address": device_ip}
        if code is not None:
            body.update({"output": "user-with-headers", "http-method": "post",
                         "http-data": urllib.parse.urlencode({"code": code}),
                         "http-header-field": "Content-Type:application/x-www-form-urlencoded"})
        return client("/tool/fetch", "POST", body)[-1]

    # No redirect/forwarded-header identity: a management request cannot redeem.
    response = request(urllib.request.build_opener(), "/portal/api/redeem?ip=" + device_ip,
                       "POST", {"code": codes[0]}, {"X-Forwarded-For": device_ip})
    assert response.status == 403
    assert_message(json.load(response), "error", "not-on-wifi")
    redeemed = fetch("/portal/api/redeem", codes[0])
    state = portal_state(redeemed)
    assert_message(state, "success", "online")
    assert state["time_credit"]["bought"] == 120, "redemption did not show the purchased time"
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", state["time_credit"]["resume_code"]), "purchase did not show a Resume Code"
    assert state["time_credit"]["usage_known"] and state["time_credit"]["online"]
    # REST fetch returns headers in a separate comma-delimited field, not data.
    cookie_header = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^,\r\n]+)", redeemed.get("http-headers", ""), re.I)
    assert cookie_header, "redemption did not set a Time Credit cookie"
    assert re.match(r"openpiso_time_credit=[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43};", cookie_header.group(1))
    assert "HttpOnly" in cookie_header.group(1)
    assert "SameSite=Lax" in cookie_header.group(1)
    assert "Path=/portal" in cookie_header.group(1)
    internet_target, internet = device_internet(client_address, password, device_ip)
    assert PORTAL_URL.removeprefix("http://") + "/portal" not in internet
    state = json.load(request(opener, "/admin/state"))["purchases"]
    assert len(state["time_credits"]) == 1
    assert len(state["audit"]) == 1
    time_credit = time_credit_for_device(state, _lab_module.DEVICE_MAC, device_ip)
    assert time_credit["bought_seconds"] == 120
    audit = state["audit"][0]
    assert (audit["before_seconds"], audit["after_seconds"], audit["reason"]) == (0, 120, "voucher-redemption")
    users = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + time_credit["id"])
    assert len(users) == 1
    assert duration_seconds(users[0]["limit-uptime"]) == 120
    assert users[0]["profile"] == "openpiso-no-idle"
    time.sleep(3)
    state = portal_state(fetch("/portal/api/state?mac=00:00:00:00:00:00&ip=127.0.0.1"))
    remaining = state["time_credit"]["remaining"]
    assert 0 < remaining < 120, state
    report.snapshot("voucher-redeemed-internet-and-cookie-free-remaining", address, password, opener,
                    remaining_seconds=remaining, portal_state=state, internet_reachable=True,
                    internet_target=internet_target, signed_httponly_cookie_issued=True)
    # End one real session, then connect again to exercise committed + live usage.
    active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit["id"])
    assert len(active) == 1
    router_rest(address, "admin", password, "/ip/hotspot/active/" + active[0][".id"], "DELETE")
    committed = duration_seconds(router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + time_credit["id"])[0]["uptime"])
    assert committed >= 2
    connected = client("/tool/fetch", "POST", {"url": PORTAL_URL + "/portal/api/connect", "output": "user",
                       "src-address": device_ip, "http-method": "post", "http-data": ""})
    assert_message(portal_state(connected[-1]), "success", "online")
    time.sleep(3)
    state = portal_state(fetch("/portal/api/state"))
    remaining = state["time_credit"]["remaining"]
    assert 0 < remaining < 120 - committed
    active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit["id"])
    assert abs(remaining - duration_seconds(active[0]["session-time-left"])) <= 3
    report.snapshot("remaining-includes-committed-and-live-router-uptime", address, password, opener,
                    committed_seconds=committed, remaining_seconds=remaining, portal_state=state)
    for code, expected in ((codes[0], "already used"), ("IIIIIIII", "invalid"), ("ZZZZZZZZ", "not found")):
        try:
            fetch("/portal/api/redeem", code)
        except urllib.error.HTTPError as error:
            # RouterOS wraps the Controller's status in its own REST error.
            assert error.code == 400
        else:
            raise AssertionError("Voucher failure was accepted: " + expected)
    assert len(json.load(request(opener, "/admin/state"))["purchases"]["audit"]) == 1
    report.snapshot("used-invalid-unknown-vouchers-do-not-credit", address, password, opener)
    return device_ip, cookie_header.group(1).split(";", 1)[0]


def pause_device_fetch(client_address, password, device_ip, path, form=None, cookie=""):
    body = {"url": PORTAL_URL + path, "output": "user-with-headers",
            "src-address": device_ip}
    headers = []
    if form is not None:
        body.update({"http-method": "post", "http-data": urllib.parse.urlencode(form)})
        headers.append("Content-Type:application/x-www-form-urlencoded")
    if cookie:
        headers.append("Cookie:" + cookie)
    if headers:
        body["http-header-field"] = ",".join(headers)
    return router_rest(client_address, "admin", password, "/tool/fetch", "POST", body, scheme="http")[-1]


def device_redeem_refused(client_address, password, device_ip, code, status):
    """Redeem a code from the Device and assert the Portal refused it with status.

    RouterOS /tool/fetch fails on a non-2xx reply and keeps only the status, not the
    Portal's JSON message, so a refusal can be told apart only by its status here."""
    try:
        pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code})
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        assert f"Status {status}" in detail, detail
        return
    raise AssertionError(f"redeeming {code!r} was not refused")


def portal_state(result):
    """The Portal API's JSON state from a Device's /tool/fetch result row."""
    return json.loads(result["data"])


def vendo_ids(state):
    """The IDs of the Vendos a Portal state offers, in picker order."""
    return [vendo["id"] for vendo in state["vendos"]]


def assert_message(state, kind, code):
    message = state["message"]
    assert message and (message["kind"], message["code"]) == (kind, code) and message["text"], state


def time_credit_for_device(state, mac: str, ip: str):
    """Find exactly one Device binding in admin state or its purchases section."""
    purchases = state.get("purchases", state)
    matches = [tc for tc in purchases["time_credits"] if tc["mac"].upper() == mac.upper() and tc["ip"] == ip]
    assert len(matches) == 1, f"expected exactly one Time Credit for Device MAC {mac} IP {ip}; found {len(matches)}"
    return matches[0]


def ledger_and_router(opener, address, password, time_credit_id):
    """Everything a Portal read must leave untouched: the ledger and the Time Credit's Router rows."""
    state = json.load(request(opener, "/admin/state"))
    del state["reconciliation"]  # the Reconciler's own poll clock
    user = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + time_credit_id)
    active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit_id)
    return state, user, [a[".id"] for a in active]


def tapped_pause(opener, report, address, password, client_address, device_ip, cookie):
    assert request(opener, "/admin/pause", "POST", {
        "pause_limit": "2", "idle_seconds": "120", "idle_enabled": "off"}).status == 200
    state = json.load(request(opener, "/admin/state"))
    time_credit = time_credit_for_device(state, _lab_module.DEVICE_MAC, device_ip)
    user_path = "/ip/hotspot/user?name=op-" + time_credit["id"]
    active_path = "/ip/hotspot/active?user=op-" + time_credit["id"]

    def fetch(path, form=None, proof=""):
        return pause_device_fetch(client_address, password, device_ip, path, form, proof)

    # The latest Top-up sets the Pause Limit (ADR 0006), so a Voucher applies the new 2.
    response = generate_vouchers(opener, {"seconds": "30", "count": "1"})
    code = voucher_codes(opener, response)[0]
    assert portal_state(fetch("/portal/api/redeem", {"code": code}, cookie))["time_credit"]["pause_limit"] == 2

    def refused(path, proof=""):
        try:
            fetch(path, {}, proof)
        except urllib.error.HTTPError as error:
            assert error.code == 400  # RouterOS wraps non-2xx HTTP responses.
        else:
            raise AssertionError("Pause ownership/limit check was bypassed: " + path)

    for left in (1, 0):
        state = portal_state(fetch("/portal/api/pause", {}))  # Cookie-free Pause is allowed.
        assert_message(state, "success", "paused")
        assert state["time_credit"]["paused"] and state["time_credit"]["pauses_left"] == left
        assert state["time_credit"]["pause_limit"] == 2 and not state["time_credit"]["online"]
        assert "resume_code" not in state["time_credit"], "Resume Code shown while tapped-Paused"
        assert not router_rest(address, "admin", password, active_path)
        used = duration_seconds(router_rest(address, "admin", password, user_path)[0]["uptime"])
        # Same browser, cookie-free visits, and Connect must not resume.
        for proof in (cookie, ""):
            visit = fetch("/portal/api/state", proof=proof)
            state = portal_state(visit)
            assert state["time_credit"]["paused"] and "resume_code" not in state["time_credit"]
            assert "set-cookie:" not in visit.get("http-headers", "").lower(), "Paused visit minted ownership proof"
        refused("/portal/api/connect")
        refused("/portal/api/resume")
        refused("/portal/api/resume", cookie[:-1] + ("A" if cookie[-1] != "A" else "B"))
        internet = router_rest(client_address, "admin", password, "/tool/fetch", "POST", {
            "url": "http://1.1.1.1/", "output": "user", "src-address": device_ip}, scheme="http")
        assert "/portal" in internet[-1].get("data", ""), "Router silently logged paused Device back in"
        time.sleep(3)
        assert not router_rest(address, "admin", password, active_path)
        after = duration_seconds(router_rest(address, "admin", password, user_path)[0]["uptime"])
        assert after == used, "used uptime changed during tapped Pause"
        report.snapshot("tapped-pause-preserves-time-" + str(left), address, password, opener,
                        used_before_seconds=used, used_after_seconds=after, pauses_left=left,
                        portal_state=state, no_automatic_login=True)
        state = portal_state(fetch("/portal/api/resume", {}, cookie))
        assert_message(state, "success", "online")
        assert state["time_credit"]["online"] and not state["time_credit"]["paused"]
        assert state["time_credit"]["resume_code"]
        assert len(router_rest(address, "admin", password, active_path)) == 1
    refused("/portal/api/pause")
    assert len(router_rest(address, "admin", password, active_path)) == 1
    report.snapshot("pause-limit-refuses-without-disconnecting", address, password, opener)


def idle_pause(opener, report, address, password, client_address, device_ip):
    for form in ({"pause_limit": "-1", "idle_enabled": "on", "idle_seconds": "5"},
                 {"pause_limit": "2", "idle_enabled": "on", "idle_seconds": "0"},
                 {"pause_limit": "2", "idle_enabled": "maybe", "idle_seconds": "5"}):
        assert request(opener, "/admin/pause", "POST", form).status == 400
    settings = {"pause_limit": "2", "idle_enabled": "on", "idle_seconds": "5"}
    assert request(opener, "/admin/pause", "POST", settings).status == 200
    state = json.load(request(opener, "/admin/state"))
    time_credit = time_credit_for_device(state, _lab_module.DEVICE_MAC, device_ip)
    assert state["pause_settings"] == {"pause_limit": 2, "idle_enabled": True, "idle_seconds": 5}
    user_path = "/ip/hotspot/user?name=op-" + time_credit["id"]
    active_path = "/ip/hotspot/active?user=op-" + time_credit["id"]
    assert router_rest(address, "admin", password, user_path)[0]["profile"] == "openpiso-idle"
    profiles = {p["name"]: p for p in router_rest(address, "admin", password, "/ip/hotspot/user/profile")}
    assert duration_seconds(profiles["openpiso-idle"]["idle-timeout"]) == 5
    assert profiles["openpiso-no-idle"]["idle-timeout"] == "none"
    assert profiles["openpiso-idle"]["keepalive-timeout"] == "none"
    # Fixture traffic generator must not keep the customer Device active.
    for scheduler in router_rest(client_address, "admin", password, "/system/scheduler?name=ping-gen", scheme="http"):
        router_rest(client_address, "admin", password, "/system/scheduler/" + scheduler[".id"], "PATCH", {"disabled": "yes"}, scheme="http")
    for iteration in range(2):
        deadline = time.monotonic() + 20
        while router_rest(address, "admin", password, active_path):
            assert time.monotonic() < deadline, "Idle Pause did not stop the Device"
            time.sleep(1)
        used = duration_seconds(router_rest(address, "admin", password, user_path)[0]["uptime"])
        time.sleep(3)
        after = duration_seconds(router_rest(address, "admin", password, user_path)[0]["uptime"])
        assert used == after, "used uptime changed while Idle Paused"
        # Opening the Portal only reads: two state reads change nothing anywhere.
        before = ledger_and_router(opener, address, password, time_credit["id"])
        for _ in range(2):
            idle = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state"))
            assert idle["message"] is None, idle
            credit = idle["time_credit"]
            assert not credit["online"] and not credit["paused"] and credit["remaining"] > 0
            assert credit["pauses_left"] == 0 and credit["pause_limit"] == 2
        assert ledger_and_router(opener, address, password, time_credit["id"]) == before, "state read changed ledger or Router"
        assert not router_rest(address, "admin", password, active_path), "state read logged the Device in"
        # The Portal's automatic reconnect is the POST connect.
        state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/connect", {}))
        assert_message(state, "success", "online")
        assert state["time_credit"]["online"] and state["time_credit"]["pauses_left"] == 0
        assert len(router_rest(address, "admin", password, active_path)) == 1
        state = json.load(request(opener, "/admin/state"))
        pause = next(p for p in state["pauses"] if p["time_credit_id"] == time_credit["id"])
        assert pause["tapped_count"] == 2 and not pause["tapped"]
        report.snapshot("idle-pause-read-only-state-then-cookie-free-connect-" + str(iteration), address, password, opener,
                        used_before_seconds=used, used_after_seconds=after, idle_state=idle,
                        state_reads_changed_nothing=True, idle_pauses_do_not_consume_limit=True)
    assert request(opener, "/admin/pause", "POST", {
        "pause_limit": "2", "idle_enabled": "off", "idle_seconds": "7"}).status == 200
    assert router_rest(address, "admin", password, user_path)[0]["profile"] == "openpiso-no-idle"
    profiles = {p["name"]: p for p in router_rest(address, "admin", password, "/ip/hotspot/user/profile")}
    assert duration_seconds(profiles["openpiso-idle"]["idle-timeout"]) == 7
    time.sleep(9)
    assert len(router_rest(address, "admin", password, active_path)) == 1, "Idle Pause remained enabled"
    report.snapshot("idle-pause-disabled-and-profiles-updated", address, password, opener)


def cookie_after_restart(client_address, password, device_ip, cookie):
    result = router_rest(client_address, "admin", password, "/tool/fetch", "POST", {
        "url": PORTAL_URL + "/portal/api/connect", "output": "user-with-headers",
        "src-address": device_ip, "http-method": "post", "http-data": "",
        "http-header-field": "Cookie:" + cookie,
    }, scheme="http")
    state = portal_state(result[-1])
    assert state["time_credit"]["bought"] == 120
    assert_message(state, "success", "online")
    issued = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", result[-1].get("http-headers", ""), re.I)
    assert issued and issued.group(1) == cookie, "per-install signing key/token changed on restart"


def reconciler_restart_outage(controller, args, data_dir, opener, report, address, password,
                              client_address, device_ip):
    before = json.load(request(opener, "/admin/state"))
    tc = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
    assert len(active) == 1
    session_id = active[0][".id"]
    used_before = duration_seconds(active[0]["uptime"])
    committed_before = duration_seconds(router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])[0]["uptime"])
    controller.stop()
    # The Router-hosted page must survive the Controller. The default visible
    # state is unavailable; only a successful ready script permits redirect.
    page = router_rest(address, "admin", password, "/file?name=hotspot%2Flogin.html")[0]["contents"]
    assert "temporarily unavailable" in page
    assert "/portal/ready.js" in page and "onload" in page
    assert 'http-equiv="refresh"' not in page
    assert '<form' not in page
    for path, method in (("/portal/ready.js", "GET"), ("/portal/api/state", "GET"),
                         ("/portal/api/connect", "POST"), ("/portal/api/redeem", "POST")):
        try:
            urllib.request.urlopen(urllib.request.Request(BASE_URL + path, method=method), timeout=2)
        except OSError:
            pass
        else:
            raise AssertionError("stopped Controller accepted " + path)
    internet_target, internet = device_internet(client_address, password, device_ip)
    assert "temporarily unavailable" not in internet and "/portal" not in internet
    time.sleep(3)
    during = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
    assert len(during) == 1 and during[0][".id"] == session_id
    assert duration_seconds(during[0]["uptime"]) > used_before
    report.snapshot("controller-outage-retains-router-session", address, password,
                    outage_page_html=page, existing_device_internet=True, internet_target=internet_target)
    controller = Controller(args.binary, data_dir, initialize=False)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = json.load(request(opener, "/admin/state"))
            if state["reconciliation"]["last_poll"]:
                break
            time.sleep(1)
        assert state["purchases"] == before["purchases"]
        assert state["reconciliation"]["last_poll"]
        assert any(a["time_credit_id"] == tc["id"] for a in state["reconciliation"]["active"])
        time.sleep(11)
        later = json.load(request(opener, "/admin/state"))
        assert later["reconciliation"]["last_poll"] > state["reconciliation"]["last_poll"]
        after = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
        assert len(after) == 1 and after[0][".id"] == session_id, "boot/poll disrupted healthy session"
        assert duration_seconds(after[0]["uptime"]) >= duration_seconds(during[0]["uptime"])
        assert duration_seconds(router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])[0]["limit-uptime"]) == tc["bought_seconds"]
        committed_after = duration_seconds(router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])[0]["uptime"])
        assert committed_after == committed_before
        remaining = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state"))["time_credit"]["remaining"]
        assert abs(remaining - (tc["bought_seconds"] - committed_after - duration_seconds(after[0]["uptime"]))) <= 3
        assert request(opener, "/portal/ready.js").status == 200
        report.snapshot("boot-and-ten-second-poll-preserve-meter-and-ledger", address, password, opener,
                        same_router_session=True, bought_time_unchanged=True, remaining_seconds=remaining,
                        committed_seconds=committed_after)
        return controller, opener
    except BaseException:
        report.capture_lab_failure()
        controller.stop()
        raise


def top_up_online(opener, report, address, password, client_address, device_ip, cookie):
    response = generate_vouchers(opener, {"seconds": "15", "count": "1"})
    assert response.status == 200
    code = voucher_codes(opener, response)[0]
    before = json.load(request(opener, "/admin/state"))
    tc = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    validity_before = next(v for v in before["validity"] if v["time_credit_id"] == tc["id"])
    intent_before = next(i for i in before["credit_intents"] if i["time_credit_id"] == tc["id"])
    pause_before = next(p for p in before["pauses"] if p["time_credit_id"] == tc["id"])
    assert pause_before["tapped_count"] == 2 and not pause_before["tapped"]
    assert not intent_before["pending"]
    user_path = "/ip/hotspot/user?name=op-" + tc["id"]
    active_path = "/ip/hotspot/active?user=op-" + tc["id"]
    user_before = router_rest(address, "admin", password, user_path)[0]
    active_before = router_rest(address, "admin", password, active_path)
    assert len(active_before) == 1
    used_before = duration_seconds(user_before["uptime"]) + duration_seconds(active_before[0]["uptime"])
    started = time.monotonic()
    result = pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}, cookie)
    state = portal_state(result)
    total = tc["bought_seconds"] + 15
    assert state["time_credit"]["bought"] == total
    assert_message(state, "success", "online")
    assert state["time_credit"]["pauses_left"] == 2
    issued = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", result.get("http-headers", ""), re.I)
    assert issued and issued.group(1) == cookie, "Top-up replaced the Time Credit token"
    after = json.load(request(opener, "/admin/state"))
    purchases = after["purchases"]
    assert len(purchases["time_credits"]) == len(before["purchases"]["time_credits"])
    validity = next(v for v in after["validity"] if v["time_credit_id"] == tc["id"])
    assert validity["valid_until"] > validity_before["valid_until"]
    assert abs(validity["valid_until"] - (time.time() + 30 * 86400)) <= 5
    assert validity["expired_at"] == 0 and not validity["router_removed"]
    assert state["time_credit"]["valid_until"] == validity["valid_until"]
    updated = next(item for item in purchases["time_credits"] if item["id"] == tc["id"])
    assert updated == dict(tc, bought_seconds=total)
    assert len(purchases["audit"]) == len(before["purchases"]["audit"]) + 1
    audit = purchases["audit"][-1]
    assert (audit["time_credit_id"], audit["source"], audit["before_seconds"], audit["after_seconds"]) == (tc["id"], code, tc["bought_seconds"], total)
    voucher = next(v for v in purchases["vouchers"] if v["code"] == code)
    assert voucher["time_credit_id"] == tc["id"] and voucher["redeemed_at"]
    intent = next(i for i in after["credit_intents"] if i["time_credit_id"] == tc["id"])
    assert len(after["credit_intents"]) == len(before["credit_intents"])
    assert intent["revision"] == intent_before["revision"] + 1
    assert intent["bought_seconds"] == total and not intent["pending"] and not intent["error"]
    pause = next(p for p in after["pauses"] if p["time_credit_id"] == tc["id"])
    assert pause["tapped_count"] == 0 and not pause["tapped"]
    user = router_rest(address, "admin", password, user_path)[0]
    active = router_rest(address, "admin", password, active_path)
    assert user[".id"] == user_before[".id"] and duration_seconds(user["limit-uptime"]) == total
    assert len(active) == 1
    committed = duration_seconds(user["uptime"])
    live_before = duration_seconds(active_before[0]["uptime"])
    live_after = duration_seconds(active[0]["uptime"])
    # RouterOS may reuse the active-table .id after remove/login. Committed
    # usage and a fresh live clock prove the old session actually ended.
    assert committed >= used_before, "Top-up did not commit the old session's usage"
    assert live_after < live_before, "Top-up did not restart the live session clock"
    used_after = committed + live_after
    assert used_before <= used_after <= used_before + int(time.monotonic() - started) + 3, "Top-up lost or invented Router usage"
    remaining = state["time_credit"]["remaining"]
    assert abs(remaining - (total - used_after)) <= 3
    assert abs(remaining - duration_seconds(active[0]["session-time-left"])) <= 3
    internet_target, internet = device_internet(client_address, password, device_ip)
    assert "/portal" not in internet and "temporarily unavailable" not in internet
    report.snapshot("online-top-up-reuses-time-credit-and-refreshes-countdown", address, password, opener,
                    portal_state=state, remaining_seconds=remaining, committed_seconds=committed,
                    committed_before_seconds=duration_seconds(user_before["uptime"]),
                    live_before_seconds=live_before, live_after_seconds=live_after,
                    countdown_before_seconds=duration_seconds(active_before[0]["session-time-left"]),
                    countdown_after_seconds=duration_seconds(active[0]["session-time-left"]),
                    active_entry_id_reused=active[0][".id"] == active_before[0][".id"],
                    router_session_replaced=True, internet_reachable=True, internet_target=internet_target,
                    signed_cookie_unchanged=True)
    return code


def top_up_pause_budget(opener, report, address, password, client_address, device_ip, cookie, used_code):
    # The old budget was exhausted. Top-up must actually permit another Pause,
    # not merely display a reset count. A rejected Voucher must not reset it.
    state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/pause", {}))
    assert state["time_credit"]["paused"] and state["time_credit"]["pauses_left"] == 1
    before = json.load(request(opener, "/admin/state"))
    try:
        pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": used_code}, cookie)
    except urllib.error.HTTPError as error:
        assert "409" in json.load(error).get("detail", "")
    else:
        raise AssertionError("Top-up Voucher was consumed twice")
    after = json.load(request(opener, "/admin/state"))
    for key in ("purchases", "credit_intents", "pauses"):
        assert after[key] == before[key], "rejected Top-up changed " + key
    tc = time_credit_for_device(after, _lab_module.DEVICE_MAC, device_ip)
    assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
    state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/resume", {}, cookie))
    assert_message(state, "success", "online")
    assert state["time_credit"]["pauses_left"] == 1
    report.snapshot("top-up-restores-pause-budget-and-replay-does-not-reset-it", address, password, opener,
                    portal_state=state, voucher_replay_rejected=True)


def time_running_out(opener, report, address, password, client_address, device_ip):
    before = json.load(request(opener, "/admin/state"))["purchases"]
    tc = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    deadline = time.monotonic() + tc["bought_seconds"] + 15
    while time.monotonic() < deadline:
        active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
        if not active:
            break
        # Real traffic keeps this a running-out test, not Idle Pause.
        router_rest(client_address, "admin", password, "/ping", "POST", {
            "address": "10.5.50.1", "src-address": device_ip, "count": "1"}, scheme="http")
        time.sleep(2)
    assert not active, "Router did not enforce limit-uptime"
    user = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])[0]
    assert duration_seconds(user["uptime"]) >= tc["bought_seconds"]
    redirect = router_rest(client_address, "admin", password, "/tool/fetch", "POST", {
        "url": "http://1.1.1.1/", "src-address": device_ip, "output": "user"}, scheme="http")[-1]["data"]
    assert "/portal" in redirect and "/portal/ready.js" in redirect
    state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state"))
    assert state["time_credit"]["usage_known"] and state["time_credit"]["remaining"] == 0
    assert not state["time_credit"]["online"]
    assert_message(state, "warning", "time-up")
    assert json.load(request(opener, "/admin/state"))["purchases"] == before
    report.snapshot("router-exhaustion-redirects-with-no-invented-time", address, password, opener,
                    portal_state=state, redirect_html=redirect, router_limit_enforced=True)


def pending_credit_recovery(controller, args, data_dir, opener, report, address, password,
                            client_address, device_ip):
    response = generate_vouchers(opener, {"seconds": "15", "count": "1"})
    code = voucher_codes(opener, response)[0]
    before_state = json.load(request(opener, "/admin/state"))
    before = before_state["purchases"]
    existing = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    intent_before = next(i for i in before_state["credit_intents"] if i["time_credit_id"] == existing["id"])
    pause_before = next(p for p in before_state["pauses"] if p["time_credit_id"] == existing["id"])
    assert pause_before["tapped_count"] == 1 and not pause_before["tapped"]
    group = router_rest(address, "admin", password, "/user/group?name=openpiso-controller")[0]
    user = router_rest(address, "admin", password, "/user?name=openpiso-controller")[0]
    assert user["disabled"] == "false"
    # RouterOS PATCH needs !write, not omission. Existing REST authentication
    # sessions retain their policy until disabling the API user invalidates them.
    # Device reads must still work, while the credit projection must fail.
    restricted = ",".join("!write" if p == "write" else p for p in group["policy"].split(","))
    try:
        router_rest(address, "admin", password, "/user/group/" + group[".id"], "PATCH", {"policy": restricted})
        for disabled in ("yes", "no"):
            router_rest(address, "admin", password, "/user/" + user[".id"], "PATCH", {"disabled": disabled})
        policy = router_rest(address, "admin", password, "/user/group?name=openpiso-controller")[0]["policy"]
        assert "write" not in policy.split(","), "fixture did not revoke Router write permission"
        try:
            pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code})
        except urllib.error.HTTPError as error:
            assert "503" in json.load(error).get("detail", "")
        else:
            raise AssertionError("Router write failure was not surfaced")
        state = json.load(request(opener, "/admin/state"))
        pending = [intent for intent in state["credit_intents"] if intent["pending"]]
        assert len(pending) == 1
        intent = pending[0]
        assert intent["error"] == "Router projection failed; retry pending"
        assert len(state["purchases"]["audit"]) == len(before["audit"]) + 1
        tc = next(tc for tc in state["purchases"]["time_credits"] if tc["id"] == intent["time_credit_id"])
        assert tc == dict(existing, bought_seconds=existing["bought_seconds"] + 15)
        assert intent["revision"] == intent_before["revision"] + 1
        assert intent["bought_seconds"] == tc["bought_seconds"]
        pause = next(p for p in state["pauses"] if p["time_credit_id"] == tc["id"])
        assert pause["tapped_count"] == 0 and not pause["tapped"]
        users = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])
        assert duration_seconds(users[0]["limit-uptime"]) == tc["bought_seconds"] - 15
        admin_page = request(opener, "/admin/intents").read().decode()
        assert tc["id"] in admin_page and "pending" in admin_page
        report.snapshot("router-error-keeps-committed-credit-pending", address, password, opener,
                        pending_intent=intent, router_write_denied=True)
        controller.kill()
    finally:
        report.capture_lab_failure()
        router_rest(address, "admin", password, "/user/group/" + group[".id"], "PATCH", {"policy": group["policy"]})
        for disabled in ("yes", "no"):
            router_rest(address, "admin", password, "/user/" + user[".id"], "PATCH", {"disabled": disabled})
    controller = Controller(args.binary, data_dir, initialize=False)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = json.load(request(opener, "/admin/state"))
            if not any(i["pending"] for i in state["credit_intents"]):
                break
            time.sleep(1)
        assert not any(i["pending"] for i in state["credit_intents"])
        users = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])
        assert len(users) == 1
        assert duration_seconds(users[0]["limit-uptime"]) == tc["bought_seconds"]
        assert len(state["purchases"]["audit"]) == len(before["audit"]) + 1
        assert next(p for p in state["pauses"] if p["time_credit_id"] == tc["id"]) == pause
        time.sleep(11)
        assert json.load(request(opener, "/admin/state"))["purchases"] == state["purchases"]
        assert duration_seconds(router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])[0]["limit-uptime"]) == tc["bought_seconds"]
        report.snapshot("kill-after-credit-commit-replays-one-absolute-limit", address, password, opener,
                        one_effective_limit_seconds=tc["bought_seconds"], audit_credited_once=True)
        return controller, opener
    except BaseException:
        report.capture_lab_failure()
        controller.stop()
        raise


def wait_for_validity_removal(opener, address, password, time_credit_id, valid_until):
    deadline = time.monotonic() + max(0, valid_until - time.time()) + 25
    while time.monotonic() < deadline:
        state = json.load(request(opener, "/admin/state"))
        validity = next(v for v in state["validity"] if v["time_credit_id"] == time_credit_id)
        users = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + time_credit_id)
        active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit_id)
        if validity["expired_at"] and validity["router_removed"] and not users and not active:
            return state
        time.sleep(1)
    raise AssertionError("Validity expired but Time Credit/Router removal did not finish")


def validity_online_expiry(opener, report, address, password, client_address, device_ip):
    # A Top-up never shortens Validity (ADR 0006), so Rebind the Device's long-lived
    # Time Credit to the lan2 Device; the Vouchers below then start a fresh one here.
    held = portal_state(rebind_fetch(client_address, password, device_ip))["time_credit"]
    assert held, "the Device should still hold its long-lived Time Credit"
    second_ip = rebind_wait_device(client_address, password, "lan2")
    moved = rebind_fetch(client_address, password, second_ip, "/portal/api/recover", {"code": held["resume_code"]})
    # Earlier scenarios may have used up its time; Recover still moves it.
    if held["remaining"]:
        assert_message(portal_state(moved), "success", "online")
    else:
        assert_message(portal_state(moved), "warning", "time-up")
    assert portal_state(rebind_fetch(client_address, password, device_ip))["time_credit"] is None
    # Thirty real seconds through the same whole-number form an operator uses.
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "seconds"}).status == 200
    response = generate_vouchers(opener, {"seconds": "120", "count": "3"})
    assert response.status == 200
    codes = voucher_codes(opener, response)
    assert len(codes) == 3
    first = pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[0]})
    assert_message(portal_state(first), "success", "online")
    cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", first.get("http-headers", ""), re.I).group(1)
    state = json.load(request(opener, "/admin/state"))
    voucher = next(v for v in state["purchases"]["vouchers"] if v["code"] == codes[0])
    time_credit_id = voucher["time_credit_id"]
    original = next(v for v in state["validity"] if v["time_credit_id"] == time_credit_id)
    time.sleep(8)
    topped_up = pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[1]}, cookie)
    assert_message(portal_state(topped_up), "success", "online")
    state = json.load(request(opener, "/admin/state"))
    validity = next(v for v in state["validity"] if v["time_credit_id"] == time_credit_id)
    assert validity["valid_until"] >= original["valid_until"] + 8
    assert abs(validity["valid_until"] - (time.time() + 30)) <= 5
    # Changing site policy must not rewrite an already purchased deadline.
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200
    time.sleep(max(0, original["valid_until"] + 1 - time.time()))
    surviving = json.load(request(opener, "/admin/state"))
    current = next(v for v in surviving["validity"] if v["time_credit_id"] == time_credit_id)
    assert current == validity, "expiry used the deadline from before Top-up"
    assert router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit_id)
    report.snapshot("top-up-validity-wins-over-old-deadline", address, password, opener,
                    previous_valid_until=original["valid_until"], extended_valid_until=validity["valid_until"])
    state = wait_for_validity_removal(opener, address, password, time_credit_id, validity["valid_until"])
    expiry_audit = [a for a in state["purchases"]["audit"] if a["time_credit_id"] == time_credit_id and a["reason"] == "validity-expiry"]
    assert len(expiry_audit) == 1 and expiry_audit[0]["after_seconds"] == 0
    expired_state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state", cookie=cookie))
    assert expired_state["time_credit"] is None, "forfeited time still shown"
    assert expired_state["rates"], "the out state must still offer ways to buy"
    try:
        pause_device_fetch(client_address, password, device_ip, "/portal/api/connect", {}, cookie)
    except urllib.error.HTTPError as error:
        assert "403" in json.load(error).get("detail", "")
    else:
        raise AssertionError("expired cookie recreated a Router user")
    redirect = router_rest(client_address, "admin", password, "/tool/fetch", "POST", {
        "url": "http://1.1.1.1/", "src-address": device_ip, "output": "user"}, scheme="http")[-1]["data"]
    assert "/portal" in redirect
    report.snapshot("online-validity-forfeits-unused-time-and-removes-router-user", address, password, opener,
                    portal_state=expired_state, router_user_removed=True, redirect_html=redirect)
    return time_credit_id, cookie, codes[2]


def admin_expired_grant(opener, report, address, password, expired_id):
    before = json.load(request(opener, "/admin/state"))
    retired = next(v for v in before["validity"] if v["time_credit_id"] == expired_id)
    assert retired["expired_at"] and retired["router_removed"]
    pending = next(row for row in before["audit_review"] if row["status"] == "pending-review")
    form = {"time_credit_id": expired_id, "seconds": "10", "reason": "Expired target compensation",
            "request_id": "e2e-expired-admin-grant"}
    for grant in (form, dict(form, audit_id=pending["id"])):
        response = request(opener, "/admin/grants", "POST", grant)
        assert response.status == 409 and "Validity has ended" in response.read().decode()
        after = json.load(request(opener, "/admin/state"))
        for key in ("purchases", "validity", "credit_intents", "pauses", "audit_review"):
            assert after[key] == before[key], "refused expired grant changed " + key
    assert not router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + expired_id)
    assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + expired_id)
    report.snapshot("admin-grant-to-expired-time-credit-refused-without-mutation", address, password, opener,
                    expired_time_credit_id=expired_id, pending_audit_id=pending["id"],
                    plain_and_audit_linked_grants_refused=True, ledger_unchanged=True,
                    router_user_still_removed=True)


def validity_offline_expiry(opener, report, address, password, client_address, device_ip,
                            expired_id, expired_cookie, code):
    assert request(opener, "/admin/validity", "POST", {"value": "20", "unit": "seconds"}).status == 200
    before = json.load(request(opener, "/admin/state"))
    result = pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}, expired_cookie)
    assert_message(portal_state(result), "success", "online")
    cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", result.get("http-headers", ""), re.I).group(1)
    assert cookie != expired_cookie
    state = json.load(request(opener, "/admin/state"))
    voucher = next(v for v in state["purchases"]["vouchers"] if v["code"] == code)
    new_id = voucher["time_credit_id"]
    assert new_id != expired_id
    assert len(state["purchases"]["time_credits"]) == len(before["purchases"]["time_credits"]) + 1
    tc = next(tc for tc in state["purchases"]["time_credits"] if tc["id"] == new_id)
    assert tc["bought_seconds"] == 120, "new purchase carried forfeited time forward"
    assert next(v for v in state["validity"] if v["time_credit_id"] == expired_id) == next(v for v in before["validity"] if v["time_credit_id"] == expired_id)
    assert not router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + expired_id)
    assert portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/pause", {}, cookie))["time_credit"]["paused"]
    assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + new_id)
    user = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + new_id)[0]
    assert duration_seconds(user["uptime"]) < tc["bought_seconds"], "offline expiry requires unused time"
    validity = next(v for v in state["validity"] if v["time_credit_id"] == new_id)
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200
    report.snapshot("purchase-after-expiry-creates-new-time-credit-then-pauses", address, password, opener,
                    expired_time_credit_id=expired_id, new_time_credit_id=new_id, new_bought_seconds=120,
                    signed_cookie_replaced=True, offline_before_expiry=True)
    after = wait_for_validity_removal(opener, address, password, new_id, validity["valid_until"])
    assert next(tc for tc in after["purchases"]["time_credits"] if tc["id"] == new_id) == tc
    assert len([a for a in after["purchases"]["audit"] if a["time_credit_id"] == new_id and a["reason"] == "validity-expiry"]) == 1
    try:
        pause_device_fetch(client_address, password, device_ip, "/portal/api/resume", {}, cookie)
    except urllib.error.HTTPError as error:
        assert "403" in json.load(error).get("detail", "")
    else:
        raise AssertionError("expired offline Time Credit resumed")
    time.sleep(11)
    assert json.load(request(opener, "/admin/state"))["purchases"] == after["purchases"], "expiry repeated its forfeiture audit"
    assert not router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + new_id)
    report.snapshot("offline-validity-removes-router-user-without-reviving-time", address, password, opener,
                    offline_expired=True, router_user_removed=True, expiry_audited_once=True)


def voucher_while_tapped_paused(opener, report, address, password, client_address, device_ip):
    response = generate_vouchers(opener, {"seconds": "60", "count": "2"})
    codes = voucher_codes(opener, response)
    first = pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[0]})
    cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", first.get("http-headers", ""), re.I).group(1)
    paused = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/pause", {}, cookie))
    before_remaining = paused["time_credit"]["remaining"]
    before = json.load(request(opener, "/admin/state"))
    time_credit_id = next(v["time_credit_id"] for v in before["purchases"]["vouchers"] if v["code"] == codes[0])
    # RouterOS fetch fails for a non-2xx response, so success here asserts 200.
    credited = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[1]}, cookie))
    assert_message(credited, "success", "time-added-paused")
    assert credited["time_credit"]["paused"] and not credited["time_credit"]["online"]
    assert credited["time_credit"]["remaining"] == before_remaining + 60
    after = json.load(request(opener, "/admin/state"))
    pause = next(p for p in after["pauses"] if p["time_credit_id"] == time_credit_id)
    assert pause["tapped"] and pause["tapped_count"] == 0
    assert next(v["time_credit_id"] for v in after["purchases"]["vouchers"] if v["code"] == codes[1]) == time_credit_id
    assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit_id)
    state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state", cookie=cookie))
    assert state["time_credit"]["paused"] and not state["time_credit"]["online"] and state["message"] is None
    assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit_id)
    report.snapshot("voucher-while-tapped-paused-credits-without-resuming", address, password, opener,
                    portal_state=credited, credited_seconds=60, remained_paused=True, response_status=200)
    # Leave the Device online: the next scenarios expect a Voucher to connect it.
    resumed = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/resume", {}, cookie))
    assert_message(resumed, "success", "online")


def admin_grant_after_deadline(opener, report, address, password, client_address, device_ip):
    # Start just after a completed poll: the deadline must pass BEFORE retirement.
    previous = json.load(request(opener, "/admin/state"))["reconciliation"]["last_poll"]
    deadline = time.monotonic() + 15
    while json.load(request(opener, "/admin/state"))["reconciliation"]["last_poll"] == previous:
        assert time.monotonic() < deadline, "Reconciler did not poll"
        time.sleep(0.1)
    assert request(opener, "/admin/validity", "POST", {"value": "3", "unit": "seconds"}).status == 200
    response = generate_vouchers(opener, {"seconds": "120", "count": "1"})
    code = voucher_codes(opener, response)[0]
    pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code})
    state = json.load(request(opener, "/admin/state"))
    time_credit_id = next(v["time_credit_id"] for v in state["purchases"]["vouchers"] if v["code"] == code)
    validity = next(v for v in state["validity"] if v["time_credit_id"] == time_credit_id)
    time.sleep(max(0, validity["valid_until"] - time.time()))
    before = json.load(request(opener, "/admin/state"))
    unswept = next(v for v in before["validity"] if v["time_credit_id"] == time_credit_id)
    assert unswept["expired_at"] == 0 and unswept["valid_until"] <= time.time(), "missed the pre-sweep interval"
    response = request(opener, "/admin/grants", "POST", {
        "time_credit_id": time_credit_id, "seconds": "10", "reason": "Deadline refusal",
        "request_id": "e2e-grant-after-deadline"})
    assert response.status == 409, "admin grant revived time after the Validity deadline"
    assert "Validity has ended" in response.read().decode()
    after = json.load(request(opener, "/admin/state"))
    for key in ("purchases", "validity", "credit_intents", "pauses", "audit_review"):
        assert after[key] == before[key], "refused deadline grant changed " + key
    report.snapshot("admin-grant-after-deadline-before-sweep-refused", address, password, opener,
                    expired_at_before_grant=0, valid_until=validity["valid_until"], status=409, ledger_unchanged=True)
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200


def second_device_up(client_address, password):
    """Enable the lan2 Device beside lan1, with a source-policy rule each. Returns [lan1 IP, lan2 IP]."""
    def client(path, method="GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")
    interface = client("/interface/ethernet?name=lan2")[0]
    client("/interface/ethernet/" + interface[".id"], "PATCH", {"disabled": "no"})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        second = client("/ip/address?interface=lan2")
        if second:
            break
        time.sleep(1)
    assert second, "second Device has no HotSpot address"
    ips = [client("/ip/address?interface=lan1")[0]["address"].split("/")[0], second[0]["address"].split("/")[0]]
    assert ips[0] != ips[1]
    if not client("/routing/table?name=openpiso-e2e-device2"):
        client("/routing/table", "PUT", {"name": "openpiso-e2e-device2", "fib": "yes"})
    if not client("/ip/route?routing-table=openpiso-e2e-device2"):
        client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": "10.5.50.1%lan2",
               "routing-table": "openpiso-e2e-device2", "comment": "openpiso-e2e-device"})
    # lan1's DHCP address may have changed since the earlier steps (slower guests
    # run longer); rebuild both source-policy rules for the current addresses.
    for rule in client("/routing/rule?comment=openpiso-e2e-device"):
        client("/routing/rule/" + rule[".id"], "DELETE")
    for ip, table in zip(ips, ("openpiso-e2e-device", "openpiso-e2e-device2")):
        client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table",
               "table": table, "comment": "openpiso-e2e-device"})
    for ip, interface, table in zip(ips, ("lan1", "lan2"), ("openpiso-e2e-device", "openpiso-e2e-device2")):
        device_fetch(client, ip, interface, table, PORTAL_URL + "/portal")
        client("/ping", "POST", {"address": "10.5.50.1", "src-address": ip, "interface": interface, "count": "1"})
    return ips


def concurrent_voucher(opener, report, address, password, client_address, code):
    def client(path, method="GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")
    ips = second_device_up(client_address, password)
    hosts = router_rest(address, "admin", password, "/ip/hotspot/host")
    macs = [next(host["mac-address"] for host in hosts if host["address"] == ip) for ip in ips]
    assert len(set(macs)) == 2, "concurrency scenario must use two real Device MACs"
    before = json.load(request(opener, "/admin/state"))["purchases"]
    barrier = threading.Barrier(2)
    def redeem(ip):
        barrier.wait()
        try:
            result = client("/tool/fetch", "POST", {"url": PORTAL_URL + "/portal/api/redeem", "output": "user",
                "src-address": ip, "http-method": "post", "http-data": urllib.parse.urlencode({"code": code}),
                "http-header-field": "Content-Type:application/x-www-form-urlencoded"})
            assert portal_state(result[-1])["time_credit"]["bought"] >= 120
            return "success"
        except urllib.error.HTTPError as error:
            assert error.code == 400
            assert "409" in json.load(error).get("detail", "")
            return "used"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, ips))
    assert sorted(results) == ["success", "used"], results
    after = json.load(request(opener, "/admin/state"))["purchases"]
    assert len(after["audit"]) == len(before["audit"]) + 1
    assert sum(tc["bought_seconds"] for tc in after["time_credits"]) == sum(tc["bought_seconds"] for tc in before["time_credits"]) + 120
    assert len([v for v in after["vouchers"] if v["code"] == code and v["time_credit_id"]]) == 1
    report.snapshot("two-devices-concurrently-redeem-exactly-one-winner", address, password, opener, outcomes=results)
    # Per-Device rate limit is independent of the signed cookie and URL parameters.
    for _ in range(5):
        try:
            client("/tool/fetch", "POST", {"url": PORTAL_URL + "/portal/api/redeem", "output": "user",
                "src-address": ips[1], "http-method": "post", "http-data": "code=IIIIIIII",
                "http-header-field": "Content-Type:application/x-www-form-urlencoded"})
        except urllib.error.HTTPError:
            pass
    try:
        client("/tool/fetch", "POST", {"url": PORTAL_URL + "/portal/api/redeem?mac=fake", "output": "user",
            "src-address": ips[1], "http-method": "post", "http-data": "code=IIIIIIII",
            "http-header-field": "Content-Type:application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as error:
        assert "429" in json.load(error).get("detail", "")
    else:
        raise AssertionError("per-Device Voucher rate limit not enforced")
    report.snapshot("voucher-redemption-rate-limited-per-device", address, password, opener)


def rebind_fetch(client_address, password, device_ip, path="/portal/api/state", form=None, cookie=None):
    body = {"url": PORTAL_URL + path, "output": "user-with-headers",
            "src-address": device_ip}
    headers = []
    if form is not None:
        body.update({"http-method": "post", "http-data": urllib.parse.urlencode(form)})
        headers.append("Content-Type:application/x-www-form-urlencoded")
    if cookie:
        headers.append("Cookie:" + cookie)
    if headers:
        body["http-header-field"] = ",".join(headers)
    return router_rest(client_address, "admin", password, "/tool/fetch", "POST", body, scheme="http")[-1]


def rebind_wait_device(client_address, password, interface="lan1", url=None):
    url = url or PORTAL_URL + "/portal"  # PORTAL_URL changes in ARM mode
    def client(path, method="GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")
    table = "openpiso-e2e-device" if interface == "lan1" else "openpiso-e2e-device2"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        leases = client("/ip/dhcp-client?interface=" + interface)
        # As in device_fetch, a renewing lease still owns its usable address.
        bound = next((lease for lease in leases
                      if lease.get("status", "").startswith(("bound", "renewing")) and lease.get("address")), None)
        if bound is not None:
            # DHCP may choose a new IP when the NIC returns. Update only this
            # Device's fixture rule; the Controller still verifies the real host.
            device_ip = bound["address"].split("/")[0]
            rules = [rule for rule in client("/routing/rule?comment=openpiso-e2e-device") if rule["table"] == table]
            assert len(rules) == 1, "Device source-policy fixture must have exactly one rule per table"
            if rules[0]["src-address"] != device_ip + "/32":
                client("/routing/rule/" + rules[0][".id"], "PATCH", {"src-address": device_ip + "/32"})
            device_fetch(client, device_ip, interface, table, url)
            return device_ip
        time.sleep(1)
    raise AssertionError("Device DHCP lease did not become usable after the NIC switch: " +
                         repr([(lease.get("status"), lease.get("address")) for lease in leases]))


def rebind_recovery(opener, report, address, password, client_address, device_ip, cookie):
    # Earlier exhaustion/recovery cases may have spent the original purchase.
    response = generate_vouchers(opener, {"seconds": "180", "count": "1"})
    top_up = voucher_codes(opener, response)[0]
    assert_message(portal_state(rebind_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": top_up}, cookie)), "success", "online")
    code = portal_state(rebind_fetch(client_address, password, device_ip))["time_credit"].get("resume_code", "")
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", code), "purchase must show a short Resume Code"
    before = json.load(request(opener, "/admin/state"))["purchases"]
    time_credit = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    assert "resume_code" not in time_credit, "recovery credentials leaked in admin readback"
    original_mac = time_credit["mac"]
    # A valid credential must not log in a caller absent from /hotspot/host.
    denied = request(urllib.request.build_opener(), "/portal/api/recover?ip=" + device_ip + "&mac=" + original_mac,
                     "POST", {"code": code}, {"X-Forwarded-For": device_ip})
    assert denied.status == 403
    assert_message(json.load(denied), "error", "not-on-wifi")
    assert json.load(request(opener, "/admin/state"))["purchases"] == before
    interface = router_rest(client_address, "admin", password, "/interface/ethernet?name=lan1", scheme="http")[0]
    second_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan2", scheme="http")[0]["address"].split("/")[0]
    changed_mac = router_rest(client_address, "admin", password, "/interface/ethernet?name=lan2", scheme="http")[0]["mac-address"]
    assert changed_mac != original_mac
    response = generate_vouchers(opener, {"seconds": "60", "count": "1"})
    second_code = voucher_codes(opener, response)[0]
    assert_message(portal_state(rebind_fetch(client_address, password, second_ip, "/portal/api/redeem", {"code": second_code})), "success", "online")
    before = json.load(request(opener, "/admin/state"))["purchases"]
    displaced = time_credit_for_device(before, changed_mac, second_ip)
    assert displaced["id"] != time_credit["id"]
    # Virtio ignores runtime MAC edits. Switch the CHR's real HotSpot NIC, as in
    # the identity lab; source-policy routes preserve the two distinct Devices.
    router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                {"disabled": "yes"}, scheme="http")
    try:
        second_ip = rebind_wait_device(client_address, password, "lan2")
        hosts = router_rest(address, "admin", password, "/ip/hotspot/host")
        assert any(h["address"] == second_ip and h["mac-address"] == changed_mac for h in hosts)
        recovered = portal_state(rebind_fetch(client_address, password, second_ip, "/portal/api/recover", {"code": code}))
        assert_message(recovered, "success", "online")
        remaining = recovered["time_credit"]["remaining"]
        assert 0 < remaining < time_credit["bought_seconds"], "Rebind reset metered uptime"
        active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit["id"])
        assert len(active) == 1 and active[0]["mac-address"] == changed_mac
        state_after = json.load(request(opener, "/admin/state"))
        after = state_after["purchases"]
        rebind_intent = next(i for i in state_after["credit_intents"] if i["time_credit_id"] == time_credit["id"])
        assert not rebind_intent["pending"] and rebind_intent["bought_seconds"] == time_credit["bought_seconds"]
        rebound = time_credit_for_device(after, changed_mac, second_ip)
        assert rebound["id"] == time_credit["id"]
        assert rebound["mac"] == changed_mac and rebound["bought_seconds"] == time_credit["bought_seconds"]
        unbound = next(tc for tc in after["time_credits"] if tc["id"] == displaced["id"])
        assert unbound["mac"] == unbound["ip"] == ""
        assert unbound["bought_seconds"] == displaced["bought_seconds"]
        assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + displaced["id"])
        restriction = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + time_credit["id"])[0]
        assert restriction["mac-address"] == changed_mac
        records = [r for r in after["rebinds"] if r["time_credit_id"] == time_credit["id"]]
        assert len(records) == 1
        assert records[0]["old_mac"] == original_mac and records[0]["new_mac"] == changed_mac
        report.snapshot("resume-code-rebind-after-real-mac-change", address, password, opener,
                        old_mac=original_mac, new_mac=changed_mac, remaining_seconds=remaining,
                        host_present_before_login=True, displaced_time_credit_id=displaced["id"],
                        displaced_binding_cleared=True, hotspot_user_mac_restriction=restriction["mac-address"])
        # Bring back the old Device; its old session must no longer grant access.
        router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                    {"disabled": "no"}, scheme="http")
        device_ip = rebind_wait_device(client_address, password)
        internet = router_rest(client_address, "admin", password, "/tool/fetch", "POST",
                              {"url": "http://1.1.1.1/", "output": "user", "src-address": device_ip}, scheme="http")[-1]["data"]
        assert PORTAL_URL.removeprefix("http://") + "/portal" in internet, "old Device retained Internet access"
        used_code = next(v["code"] for v in before["vouchers"] if v["time_credit_id"] == time_credit["id"])
        for rejected, status in ((used_code, "409"), ("IIIIIIII", "400"), ("ZZZZZZZZ", "400")):
            try:
                rebind_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": rejected}, cookie)
            except urllib.error.HTTPError as error:
                assert status in json.load(error).get("detail", "")
            else:
                raise AssertionError("invalid Voucher was accepted while presenting a new Device cookie")
            assert json.load(request(opener, "/admin/state"))["purchases"] == after, "failed Voucher moved a Time Credit"
        report.snapshot("failed-vouchers-with-new-device-cookie-do-not-rebind", address, password, opener,
                        used_invalid_unknown_rejected=True, binding_and_credit_unchanged=True)
        # State only flags the Rebind, twice over, without moving anything; core.js POSTs connect.
        before_reads = ledger_and_router(opener, address, password, time_credit["id"])
        for _ in range(2):
            automatic = portal_state(rebind_fetch(client_address, password, device_ip, cookie=cookie))
            assert automatic["rebind"] and automatic["time_credit"] is None, automatic
        assert ledger_and_router(opener, address, password, time_credit["id"]) == before_reads, "state read moved the Time Credit"
        assert json.load(request(opener, "/admin/state"))["purchases"] == after
        active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit["id"])
        assert len(active) == 1 and active[0]["mac-address"] == changed_mac
        assert portal_state(rebind_fetch(client_address, password, second_ip, "/portal/api/pause", {}))["time_credit"]["paused"]
        try:
            rebind_fetch(client_address, password, device_ip, "/portal/api/connect", {}, cookie)
        except urllib.error.HTTPError as error:
            assert "403" in json.load(error).get("detail", "")
        else:
            raise AssertionError("automatic cookie Rebind bypassed tapped Pause")
        paused = json.load(request(opener, "/admin/state"))
        pause = next(p for p in paused["pauses"] if p["time_credit_id"] == time_credit["id"])
        assert pause["tapped"]
        assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit["id"])
        unsigned = rebind_fetch(client_address, password, device_ip)
        assert "resume_code" not in json.dumps(portal_state(unsigned))
        assert "Set-Cookie:" not in unsigned.get("http-headers", "")
        connected = portal_state(rebind_fetch(client_address, password, device_ip, "/portal/api/recover", {"code": code}))
        assert_message(connected, "success", "online")
        resumed = json.load(request(opener, "/admin/state"))
        pause_after = next(p for p in resumed["pauses"] if p["time_credit_id"] == time_credit["id"])
        assert not pause_after["tapped"] and pause_after["tapped_count"] == pause["tapped_count"]
        intent = next(i for i in resumed["credit_intents"] if i["time_credit_id"] == time_credit["id"])
        assert not intent["pending"] and intent["bought_seconds"] == time_credit["bought_seconds"]
        active = router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + time_credit["id"])
        assert len(active) == 1 and active[0]["mac-address"] == original_mac
        final = json.load(request(opener, "/admin/state"))["purchases"]
        assert len([r for r in final["rebinds"] if r["time_credit_id"] == time_credit["id"]]) == 2
        report.snapshot("cookie-rebind-newest-device-wins-old-device-denied", address, password, opener,
                        old_device_denied=True, automatic_cookie_post=True, restored_device_ip=device_ip)
    finally:
        report.capture_lab_failure()
        router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                    {"disabled": "no"}, scheme="http")

    return device_ip


def admin_grant(opener, report, address, password, client_address, device_ip, cookie):
    before = json.load(request(opener, "/admin/state"))
    tc = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    form = {"time_credit_id": tc["id"], "seconds": "20", "reason": "Customer compensation",
            "request_id": "e2e-admin-grant"}
    for seconds in ("0", "-1", "31536001"):
        assert request(opener, "/admin/grants", "POST", dict(form, seconds=seconds)).status == 400
    assert request(urllib.request.build_opener(), "/admin/grants", "POST", form).status == 401
    assert request(opener, "/admin/grants", "POST", dict(form, time_credit_id="missing")).status == 404
    assert json.load(request(opener, "/admin/state"))["purchases"] == before["purchases"]
    user_path = "/ip/hotspot/user?name=op-" + tc["id"]
    active_path = "/ip/hotspot/active?user=op-" + tc["id"]
    user_before = router_rest(address, "admin", password, user_path)[0]
    active_before = router_rest(address, "admin", password, active_path)
    assert len(active_before) == 1
    used_before = duration_seconds(user_before["uptime"]) + duration_seconds(active_before[0]["uptime"])
    started = time.monotonic()
    assert request(opener, "/admin/grants", "POST", form).status == 200
    assert request(opener, "/admin/grants", "POST", form).status == 409, "form replay granted twice"
    after = json.load(request(opener, "/admin/state"))
    updated = next(item for item in after["purchases"]["time_credits"] if item["id"] == tc["id"])
    assert updated == dict(tc, bought_seconds=tc["bought_seconds"] + 20)
    validity_before = next(v for v in before["validity"] if v["time_credit_id"] == tc["id"])
    validity_after = next(v for v in after["validity"] if v["time_credit_id"] == tc["id"])
    assert validity_after["valid_until"] >= validity_before["valid_until"]
    assert abs(validity_after["valid_until"] - (time.time() + after["validity_seconds"])) <= 5
    audit = after["purchases"]["audit"][-1]
    assert audit["actor"] == "admin" and audit["reason"] == "admin-grant: Customer compensation"
    assert (audit["before_seconds"], audit["after_seconds"]) == (tc["bought_seconds"], updated["bought_seconds"])
    assert len(after["purchases"]["audit"]) == len(before["purchases"]["audit"]) + 1
    pause = next(p for p in after["pauses"] if p["time_credit_id"] == tc["id"])
    assert pause["tapped_count"] == 0 and not pause["tapped"]
    intent = next(i for i in after["credit_intents"] if i["time_credit_id"] == tc["id"])
    assert intent["bought_seconds"] == updated["bought_seconds"] and not intent["pending"]
    user = router_rest(address, "admin", password, user_path)[0]
    active = router_rest(address, "admin", password, active_path)
    assert user[".id"] == user_before[".id"] and duration_seconds(user["limit-uptime"]) == updated["bought_seconds"]
    assert len(active) == 1
    used = duration_seconds(user["uptime"]) + duration_seconds(active[0]["uptime"])
    assert used_before <= used <= used_before + int(time.monotonic() - started) + 3
    state = portal_state(rebind_fetch(client_address, password, device_ip, cookie=cookie))
    assert state["time_credit"]["bought"] == updated["bought_seconds"]
    remaining = state["time_credit"]["remaining"]
    assert abs(remaining - duration_seconds(active[0]["session-time-left"])) <= 3
    report.snapshot("admin-grant-is-a-durable-top-up-on-portal-and-router", address, password, opener,
                    portal_state=state, remaining_seconds=remaining, used_before_seconds=used_before,
                    used_after_seconds=used, duplicate_grant_refused=True)


def dashboard(opener):
    """The /admin/dashboard JSON."""
    return json.load(request(opener, "/admin/dashboard?format=json"))


def dashboard_clean_ledger(opener, report, address, password):
    # Nothing has happened yet: zero Sales, nobody online, no audit banner.
    data = dashboard(opener)
    assert data["sales"] == {"today": 0, "week": 0, "month": 0, "year": 0}
    assert (data["active_hotspot"], data["customers_with_time_left"], data["pending_audit"]) == (0, 0, 0)
    html = request(opener, "/admin/dashboard").read().decode()
    assert "audit-banner" not in html
    report.snapshot("dashboard-is-empty-without-sales-or-audit-items", address, password, opener, dashboard=data)


def dashboard_router_card(html):
    """The Router System Status card of a Dashboard page or fragment."""
    return re.search(r'<h3>Router</h3>.*?</section>', html, re.S).group(0)


def dashboard_router_status(opener, report, address, password):
    # The CHR reports CPU, memory, disk and uptime but no temperature sensor.
    data = dashboard(opener)["router"]
    assert data["unreachable"] is False
    assert 0 <= data["cpu_percent"] <= 100
    for free, total in (("memory_free_bytes", "memory_total_bytes"), ("disk_free_bytes", "disk_total_bytes")):
        assert 0 < data[free] <= data[total], free
    assert data["uptime_seconds"] > 0
    assert "temperature_c" in data and data["temperature_c"] is None
    # Independent source of truth: the Router's own totals.
    resource = router_rest(address, "admin", password, "/system/resource")
    assert data["memory_total_bytes"] == int(resource["total-memory"])
    assert data["disk_total_bytes"] == int(resource["total-hdd-space"])
    card = dashboard_router_card(request(opener, "/admin/dashboard").read().decode())
    assert re.search(r'CPU.*?\d+%', card, re.S)
    for label in ("Memory", "Disk"):
        assert re.search(label + r'.*?\d+ MB free of \d+ MB', card, re.S), label
    assert card.count('<progress') == 3
    assert re.search(r'Temperature.*?—', card, re.S)
    assert re.search(r'Uptime.*?(\d+d )?(\d+h )?\d+m', card, re.S)
    assert "Router unreachable" not in card
    report.snapshot("dashboard-router-card-reports-chr-resources", address, password, opener,
                    router=data, router_card_html=card)


def dashboard_router_unreachable(opener, report, address, password):
    # Stop the Router's REST service while the Controller keeps running, then restore it.
    rcon(password, "/ip service set www-ssl disabled=yes")
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            data = dashboard(opener)
            if data["router"]["unreachable"]:
                break
            time.sleep(1)
        else:
            raise AssertionError("Dashboard never reported the Router unreachable")
        assert data["active_hotspot"] is None
        assert all(data["router"][key] is None for key in data["router"] if key != "unreachable")
        html = request(opener, "/admin/dashboard").read().decode()
        card = dashboard_router_card(html)
        assert "Router unreachable" in card and "<progress" not in card
        assert re.search(r'Active Hotspot</p>\s*<p class="kpi-value">unknown<', html)
        # Everything that does not need the Router still renders.
        assert "This year" in html and "Customers with time left" in html and "System Status" in html
        assert data["sales"] == dashboard(opener)["sales"] and data["sales"]["year"] > 0
        assert "Router unreachable" in request(opener, "/admin/fragments/dashboard").read().decode()
    finally:
        report.capture_lab_failure()
        rcon(password, "/ip service set www-ssl disabled=no")
    outage = data
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        data = dashboard(opener)
        if not data["router"]["unreachable"] and data["active_hotspot"] is not None:
            break
        time.sleep(1)
    else:
        raise AssertionError("Dashboard did not recover after the Router came back")
    # The snapshot reads the Router over REST, so it runs once the service is back.
    report.snapshot("dashboard-says-router-unreachable-and-keeps-sales", address, password, opener,
                    dashboard_during_outage=outage, outage_html=html, dashboard_after_recovery=data)


def admin_dashboard(opener, report, address, password, client_address, device_ip, cookie):
    # Sales ranges are date-only inclusive local days: reject anything else, and reversed ranges.
    for query in ("from=not-a-date", "to=2026-13-01", "from=2026-01-01T00:00:00Z&to=2026-01-02",
                  "from=2026-01-02&to=2026-01-01"):
        assert request(opener, "/admin/sales?" + query).status == 400
    assert_login_redirect("/admin/sales")
    assert_login_redirect("/admin/sales?format=json")
    for path in ("/admin/dashboard", "/admin/dashboard?format=json", "/admin/fragments/dashboard"):
        assert_login_redirect(path)
    # The old Overview route is gone, with no redirect.
    assert request(opener, "/admin/oversight").status == 404
    assert request(opener, "/admin/fragments/time-credits").status == 404
    all_time = sales(opener)
    # #15's exact sum is checked before later Coin Window scenarios add Sales.
    assert all_time["coin_pesos"] == 4294967623
    assert all_time["unpriced_vouchers"] == 0  # New Custom batches require a price, including ₱0.
    assert all_time["total_pesos"] == all_time["voucher_pesos"] + all_time["coin_pesos"]
    # Remaining time and the two Rebinds live on the Time Credit detail page now. The usage
    # snapshot behind it is cached up to 5s, so allow that on top of the portal's own drift.
    state = json.load(request(opener, "/admin/state"))
    tc = time_credit_for_device(state, _lab_module.DEVICE_MAC, device_ip)
    detail = request(opener, "/admin/time-credits?tc=" + tc["id"]).read().decode()
    remaining = portal_state(rebind_fetch(client_address, password, device_ip, cookie=cookie))["time_credit"]["remaining"]
    listed = int(re.search(r'<progress class="wide" max="\d+" value="(\d+)"', detail).group(1))
    assert abs(listed - remaining) <= 10
    assert detail.count('<li data-kind="rebind">') == 2
    # Date-only input can't isolate a redemption, so compare today's local day before and after
    # one priced redemption (the sibling code stays unsold). The lab host and Controller share
    # a timezone, so the host's local date is the Controller's.
    now = datetime.date.today()
    today = now.isoformat()
    sales_today_before = sales(opener, today, today)
    dashboard_before = dashboard(opener)
    assert dashboard_before["sales"]["today"] == sales_today_before["total_pesos"]
    # Week (from Monday), month and year are calendar-to-date and match /admin/sales for the same days.
    starts = {"today": now, "week": now - datetime.timedelta(days=now.weekday()),
              "month": now.replace(day=1), "year": now.replace(month=1, day=1)}
    for period, start in starts.items():
        assert dashboard_before["sales"][period] == sales(opener, start.isoformat(), today)["total_pesos"], period
    response = generate_vouchers(opener, {"seconds": "5", "count": "2", "pesos": "5"})
    batch_url = voucher_print_url(response)
    codes = voucher_codes(opener, response)
    assert len(codes) == 2
    assert_message(portal_state(rebind_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[0]}, cookie)), "success", "online")
    batch_page = request(opener, batch_url).read().decode()
    sheet = json.loads(re.search(r'<script[^>]*data-voucher-batch[^>]*>(.*?)</script>', batch_page, re.S).group(1))
    assert sorted(v["used"] for v in sheet["vouchers"]) == ["", "USED"] and sheet["batch"]["pesos"] == "₱5"
    sales_today_after = sales(opener, today, today)
    for field, delta in (("voucher_pesos", 5), ("redeemed_vouchers", 1), ("unpriced_vouchers", 0),
                         ("coin_pesos", 0), ("total_pesos", 5)):
        assert sales_today_after[field] - sales_today_before[field] == delta, field
    # Inclusive whole days: a single-day range reaches the redemption, and a past day does not.
    excluded = sales(opener, "1970-01-01", "1970-01-01")
    assert (excluded["voucher_pesos"], excluded["redeemed_vouchers"], excluded["coin_pesos"], excluded["total_pesos"]) == (0, 0, 0, 0)
    dashboard_after = dashboard(opener)
    for period in starts:
        assert dashboard_after["sales"][period] - dashboard_before["sales"][period] == 5, period
    # The Router sees every online Time Credit: its HotSpot sessions are the Active Hotspot count.
    router_active = [a for a in router_rest(address, "admin", password, "/ip/hotspot/active") if a["user"].startswith("op-")]
    assert dashboard_after["active_hotspot"] == len(router_active) >= 1
    assert dashboard_after["customers_with_time_left"] >= dashboard_after["active_hotspot"]
    pending = [r for r in state["audit_review"] if r["status"] == "pending-review"]
    assert dashboard_after["pending_audit"] == len(pending) >= 3
    # Controller status: shape only, because the lab host varies. Every metric is
    # present as a number or null, and free never exceeds total.
    controller_status = dict(dashboard_after["controller"])
    # The release fields ride along (ADR 0010); a dev run has no root update helper, so no notice.
    assert isinstance(controller_status.pop("version"), str) and controller_status.pop("update") is None
    assert set(controller_status) == {"cpu_percent", "memory_free_bytes", "memory_total_bytes", "disk_free_bytes",
                                      "disk_total_bytes", "temperature_c", "uptime_seconds"}, controller_status
    for key, value in controller_status.items():
        assert value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0), key
    assert controller_status["cpu_percent"] is None or controller_status["cpu_percent"] <= 100
    for kind in ("memory", "disk"):
        free, total = controller_status[f"{kind}_free_bytes"], controller_status[f"{kind}_total_bytes"]
        assert (free is None) == (total is None) and (free is None or free <= total), kind
    html = request(opener, "/admin/dashboard").read().decode()
    card = re.search(r'<h3>Controller</h3>.*?(?=<h3>Router</h3>)', html, re.S)
    assert card, "Controller card missing"
    for label in ("CPU", "Memory", "Disk", "Temperature", "Uptime"):
        assert label in card.group(0), label
    if controller_status["temperature_c"] is None:
        assert re.search(r'Temperature</dt>\s*<dd[^>]*>—<', card.group(0))
    for text in ("Dashboard", "Today", "This week", "This month", "This year", "Active Hotspot",
                 "Customers with time left", "System Status", "Controller", "Router"):
        assert text in html, text
    # Each Sales tile links to the Sales page with its period prefilled, From through today.
    for start in starts.values():
        assert f'href="/admin/sales?from={start.isoformat()}&amp;to={today}"' in html
    # The banner counts pending coin reports and links to the Audit inbox.
    banner = re.search(r'class="audit-banner"[^>]*href="([^"]+)"[^>]*>\s*(\d+) coin reports? needs? review', html)
    assert banner and banner.group(1) == "/admin/time-credits?tab=audit" and int(banner.group(2)) == len(pending)
    assert re.search(r'href="/admin/time-credits"[^>]*data-tooltip="Grant time"', html)
    # Everything with a page of its own left the Dashboard, and so did the Overview name.
    for text in ("<table", "Online Devices", "Rebind log", "Voucher Batches", "Audit review", 'id="grant"',
                 "Sales report", "Overview", tc["id"]):
        assert text not in html, text
    # The 10-second poll is one fragment of the same view: no layout, same tiles.
    fragment = request(opener, "/admin/fragments/dashboard").read().decode()
    assert "<html" not in fragment and "System Status" in fragment and "audit-banner" in fragment
    assert 'hx-get="/admin/fragments/dashboard"' in html and 'hx-trigger="every 10s"' in html
    dashboard_router_status(opener, report, address, password)
    sales_html = request(opener, "/admin/sales").read().decode()
    # The Sales report header is one Total Sales figure (spec #73), not a per-source table.
    for text in ("Total Sales", "Showing"):
        assert text in sales_html
    assert "income" not in sales_html.lower()
    # The Sales nav item sits right under Dashboard and is highlighted on its own page.
    nav = re.findall(r'data-nav="([^"]+)"', sales_html)
    assert nav.index("/admin/sales") == nav.index("/admin/dashboard") + 1
    assert re.search(r'data-nav="/admin/sales"[^>]*data-active="true"', sales_html)
    assert re.search(r'data-nav="/admin/dashboard"[^>]*data-active="true"', html)
    report.snapshot("admin-dashboard-sales-periods-time-credit-detail-and-batch-usage", address, password, opener,
                    sales_today_before=sales_today_before, sales_today_after=sales_today_after,
                    dashboard_before=dashboard_before, dashboard_after=dashboard_after,
                    all_time_coin_pesos=all_time["coin_pesos"], remaining_seconds=remaining,
                    batch_html=batch_page, sales_html=sales_html, dashboard_html=html)


def admin_audit_review(controller, args, data_dir, opener, report, address, password,
                       client_address, device_ip, cookie):
    before = json.load(request(opener, "/admin/state"))
    rows = before["audit_review"]
    pending = [r for r in rows if r["status"] == "pending-review"]
    assert len(pending) >= 3
    assert all(not r["resolved_at"] and not r["grant_audit_id"] for r in pending)
    controller.stop()
    controller = Controller(args.binary, data_dir, initialize=False)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
    assert json.load(request(opener, "/admin/state"))["audit_review"] == rows
    tc = time_credit_for_device(before, _lab_module.DEVICE_MAC, device_ip)
    dismissed_id, granted_id, untouched_id = [r["id"] for r in pending[:3]]
    form = {"audit_id": dismissed_id, "action": "dismiss", "reason": "Inspected: no compensation due"}
    assert request(opener, "/admin/audit/resolve", "POST", dict(form, reason="")).status == 400
    assert request(opener, "/admin/audit/resolve", "POST", dict(form, action="deduct")).status == 400
    assert request(opener, "/admin/audit/resolve", "POST", dict(form, audit_id="999999999")).status == 404
    assert request(opener, "/admin/audit/resolve", "POST", form).status == 200
    assert request(opener, "/admin/audit/resolve", "POST", form).status == 409
    dismissed = json.load(request(opener, "/admin/state"))
    assert dismissed["purchases"] == before["purchases"], "dismissal changed bought time or evidence"
    row = next(r for r in dismissed["audit_review"] if r["id"] == dismissed_id)
    assert row["status"] == "dismissed" and row["resolved_by"] == "admin" and row["resolved_at"]
    assert row["resolution_reason"] == form["reason"] and row["grant_audit_id"] == 0
    # Compensating a tapped-Paused Device resets its count, not its stop intent.
    assert portal_state(rebind_fetch(client_address, password, device_ip, "/portal/api/pause", {}))["time_credit"]["paused"]
    grant = {"audit_id": granted_id, "time_credit_id": tc["id"], "seconds": "3",
             "reason": "Coin dispute compensation", "request_id": "e2e-review-grant"}
    assert request(opener, "/admin/grants", "POST", dict(grant, time_credit_id="missing")).status == 404
    assert next(r for r in json.load(request(opener, "/admin/state"))["audit_review"] if r["id"] == granted_id)["status"] == "pending-review"
    assert request(opener, "/admin/grants", "POST", grant).status == 200
    assert request(opener, "/admin/grants", "POST", dict(grant, request_id="another-form")).status == 409
    assert request(opener, "/admin/audit/resolve", "POST", dict(form, audit_id=granted_id)).status == 409
    after = json.load(request(opener, "/admin/state"))
    row = next(r for r in after["audit_review"] if r["id"] == granted_id)
    audit = after["purchases"]["audit"][-1]
    assert row["status"] == "granted" and row["resolved_by"] == "admin" and row["resolved_at"]
    assert row["grant_audit_id"] == audit["id"] and audit["time_credit_id"] == tc["id"]
    assert row["resolution_reason"] == grant["reason"]
    assert (audit["before_seconds"], audit["after_seconds"]) == (tc["bought_seconds"], tc["bought_seconds"] + 3)
    assert next(r for r in after["audit_review"] if r["id"] == untouched_id)["status"] == "pending-review"
    assert after["vendo_protocol"]["audit"] == before["vendo_protocol"]["audit"], "resolution rewrote original evidence"
    assert not router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
    state = portal_state(rebind_fetch(client_address, password, device_ip, cookie=cookie))
    assert state["time_credit"]["paused"] and state["time_credit"]["pauses_left"] == 2
    assert_message(portal_state(rebind_fetch(client_address, password, device_ip, "/portal/api/resume", {}, cookie)), "success", "online")
    controller.stop()
    controller = Controller(args.binary, data_dir, initialize=False)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
    assert json.load(request(opener, "/admin/state"))["audit_review"] == after["audit_review"]
    report.snapshot("audit-review-persists-until-explicit-dismissal-or-linked-grant", address, password, opener,
                    dismissed_audit_id=dismissed_id, granted_audit_id=granted_id,
                    grant_purchase_audit_id=audit["id"], untouched_pending_audit_id=untouched_id,
                    tapped_pause_preserved=True, duplicate_resolution_refused=True)
    return controller, opener


def resume_code_throttling(opener, report, address, password, client_address, device_ip):
    before = json.load(request(opener, "/admin/state"))["purchases"]
    for _ in range(5):
        try:
            rebind_fetch(client_address, password, device_ip, "/portal/api/recover", {"code": "IIIIIIII"})
        except urllib.error.HTTPError as error:
            assert "400" in json.load(error).get("detail", "")
        else:
            raise AssertionError("invalid Resume Code accepted")
    try:
        rebind_fetch(client_address, password, device_ip, "/portal/api/recover?mac=fake", {"code": "ZZZZZZZZ"})
    except urllib.error.HTTPError as error:
        assert "429" in json.load(error).get("detail", "")
    else:
        raise AssertionError("Resume Code guesses were not limited per Device")
    second_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan2", scheme="http")[0]["address"].split("/")[0]
    try:
        rebind_fetch(client_address, password, second_ip, "/portal/api/recover", {"code": "IIIIIIII"})
    except urllib.error.HTTPError as error:
        assert "400" in json.load(error).get("detail", ""), "one Device blocked another Device's guesses"
    else:
        raise AssertionError("invalid Resume Code accepted on second Device")
    assert json.load(request(opener, "/admin/state"))["purchases"] == before
    report.snapshot("resume-code-guesses-rate-limited-per-router-device", address, password, opener)


def mac_change_without_rebind(opener, report, address, password, client_address, device_ip):
    # Select the existing baseline profiles with the lab fixture, not #8's settings.
    response = generate_vouchers(opener, {"seconds": "180", "count": "1"})
    code = voucher_codes(opener, response)[0]
    assert_message(portal_state(rebind_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code})), "success", "online")
    state = json.load(request(opener, "/admin/state"))["purchases"]
    time_credit = time_credit_for_device(state, _lab_module.DEVICE_MAC, device_ip)
    name = "op-" + time_credit["id"]
    interface = router_rest(client_address, "admin", password, "/interface/ethernet?name=lan1", scheme="http")[0]
    second_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan2", scheme="http")[0]["address"].split("/")[0]
    rebind_count = len(state["rebinds"])
    for profile in ("openpiso-idle", "openpiso-no-idle"):
        for active in router_rest(address, "admin", password, "/ip/hotspot/active?user=" + name):
            router_rest(address, "admin", password, "/ip/hotspot/active/" + active[".id"], "DELETE")
        user = router_rest(address, "admin", password, "/ip/hotspot/user?name=" + name)[0]
        router_rest(address, "admin", password, "/ip/hotspot/user/" + user[".id"], "PATCH", {"profile": profile})
        rebind_fetch(client_address, password, device_ip)
        # Fixture re-login selects the baseline profile without building site settings.
        # The Router password is used privately and is never included in evidence.
        router_rest(address, "admin", password, "/ip/hotspot/active/login", "POST", {
            "user": name, "password": user["password"], "mac-address": time_credit["mac"], "ip": device_ip})
        active = router_rest(address, "admin", password, "/ip/hotspot/active?user=" + name)
        assert len(active) == 1
        remaining_at_departure = duration_seconds(active[0]["session-time-left"])
        profile_state = router_rest(address, "admin", password, "/ip/hotspot/user/profile?name=" + profile)[0]
        idle_period = duration_seconds(profile_state["idle-timeout"]) if profile == "openpiso-idle" else None
        assert profile_state["keepalive-timeout"] == "none"
        used_before = duration_seconds(user["uptime"])
        started = time.monotonic()
        router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                    {"disabled": "yes"}, scheme="http")
        try:
            second_ip = rebind_wait_device(client_address, password, "lan2")
            bound = idle_period if idle_period is not None else remaining_at_departure
            deadline = started + bound + 10
            while time.monotonic() < deadline:
                active = router_rest(address, "admin", password, "/ip/hotspot/active?user=" + name)
                if not active:
                    break
                time.sleep(1)
            assert not active, "departed Device exceeded its Router timeout bound"
            elapsed = time.monotonic() - started
            user_after = router_rest(address, "admin", password, "/ip/hotspot/user?name=" + name)[0]
            used_after = duration_seconds(user_after["uptime"])
            assert 0 <= used_after - used_before <= bound + 10
            if idle_period is not None:
                assert elapsed <= idle_period + 10
                time.sleep(3)
                assert router_rest(address, "admin", password, "/ip/hotspot/user?name=" + name)[0]["uptime"] == user_after["uptime"]
            after = json.load(request(opener, "/admin/state"))["purchases"]
            assert len(after["rebinds"]) == rebind_count
            report.snapshot("mac-change-without-rebind-" + profile, address, password, opener,
                            profile=profile, idle_period_seconds=idle_period,
                            departure_to_session_end_seconds=round(elapsed, 3),
                            additional_router_used_seconds=used_after - used_before,
                            remaining_at_departure_seconds=remaining_at_departure,
                            upper_bound_seconds=bound + 10, poll_resolution_seconds=1,
                            fixture_profile_selection=True, rebind_performed=False)
        finally:
            report.capture_lab_failure()
            router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                        {"disabled": "no"}, scheme="http")
        device_ip = rebind_wait_device(client_address, password)

    return device_ip


def ambiguous_device_host(opener, report, address, password, client_address, device_ip, cookie):
    before = json.load(request(opener, "/admin/state"))["purchases"]
    interface = router_rest(client_address, "admin", password, "/interface/ethernet?name=lan1", scheme="http")[0]
    second_mac = router_rest(client_address, "admin", password, "/interface/ethernet?name=lan2", scheme="http")[0]["mac-address"]
    rules = router_rest(client_address, "admin", password, "/routing/rule?comment=openpiso-e2e-device", scheme="http")
    rule = next(r for r in rules if r["src-address"] == device_ip + "/32")
    rebind_fetch(client_address, password, device_ip)
    router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                {"disabled": "yes"}, scheme="http")
    try:
        # Send from the same IP on the other real MAC. Observe Router host rows,
        # including host-dead-time, rather than inventing the ambiguity in a mock.
        router_rest(client_address, "admin", password, "/ip/address", "PUT", {
            "address": device_ip + "/32", "interface": "lan2", "comment": "openpiso-e2e-ambiguous"}, scheme="http")
        router_rest(client_address, "admin", password, "/routing/rule/" + rule[".id"], "PATCH",
                    {"table": "openpiso-e2e-device2"}, scheme="http")
        router_rest(client_address, "admin", password, "/ping", "POST", {
            "address": "10.5.50.1", "src-address": device_ip, "interface": "lan2", "count": "1"}, scheme="http")
        hosts = router_rest(address, "admin", password, "/ip/hotspot/host?address=" + device_ip)
        observed = [{key: h.get(key, "") for key in ("address", "to-address", "mac-address", "host-dead-time")} for h in hosts]
        report.snapshot("same-ip-two-mac-host-observation", address, password, opener, observed_hosts=observed)
        assert len(hosts) >= 2 and len({h["mac-address"] for h in hosts}) >= 2, "lab did not produce ambiguous host entries"
        # The second host is translated by HotSpot's address pool. Send the
        # assertion from the original NIC so the TCP peer remains device_ip.
        router_rest(client_address, "admin", password, "/routing/rule/" + rule[".id"], "PATCH",
                    {"table": rule["table"]}, scheme="http")
        for alias in router_rest(client_address, "admin", password, "/ip/address?comment=openpiso-e2e-ambiguous", scheme="http"):
            router_rest(client_address, "admin", password, "/ip/address/" + alias[".id"], "DELETE", scheme="http")
        router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                    {"disabled": "no"}, scheme="http")
        assert rebind_wait_device(client_address, password, url="http://10.5.50.1/") == device_ip, "ambiguity fixture lost the original IP"
        try:
            rebind_fetch(client_address, password, device_ip, "/portal/api/connect", {}, cookie)
        except urllib.error.HTTPError as error:
            assert "503" in json.load(error).get("detail", "")
        else:
            raise AssertionError("Controller guessed an identity from ambiguous host entries")
        assert json.load(request(opener, "/admin/state"))["purchases"] == before
        report.snapshot("ambiguous-device-host-fails-closed-without-rebind", address, password, opener,
                        observed_hosts=observed, host_dead_time_used_as_identity=False)
    finally:
        report.capture_lab_failure()
        router_rest(client_address, "admin", password, "/routing/rule/" + rule[".id"], "PATCH",
                    {"table": rule["table"]}, scheme="http")
        for alias in router_rest(client_address, "admin", password, "/ip/address?comment=openpiso-e2e-ambiguous", scheme="http"):
            router_rest(client_address, "admin", password, "/ip/address/" + alias[".id"], "DELETE", scheme="http")
        for host in router_rest(address, "admin", password, "/ip/hotspot/host?address=" + device_ip):
            if host["mac-address"] == second_mac:
                router_rest(address, "admin", password, "/ip/hotspot/host/" + host[".id"], "DELETE")
        router_rest(client_address, "admin", password, "/interface/ethernet/" + interface[".id"], "PATCH",
                    {"disabled": "no"}, scheme="http")


def portal_files(opener, prefix="/admin/portal/default/"):
    """The Default Portal's editable files, as "Download default" gives them."""
    files = {}
    for name in ("index.html", "portal.css", "custom.js"):
        response = request(opener, prefix + name)
        assert response.status == 200 and "attachment" in response.headers["Content-Disposition"], name
        files[name] = response.read().decode()
    return files


def save_portal_draft(opener, files):
    """POST a draft; return the status, the editor page and the save check's failures, entities decoded."""
    response = request(opener, "/admin/portal/draft", "POST", {
        "index_html": files["index.html"], "portal_css": files["portal.css"], "custom_js": files["custom.js"]})
    page = response.read().decode()
    failures = re.search(r'<ul class="alert">(.*?)</ul>', page, re.S)
    return response.status, html.unescape(page), [html.unescape(item) for item in
                                                  re.findall(r"<li>(.*?)</li>", failures.group(1) if failures else "")]


def portal_versions(opener):
    """The ledger's Custom Portal slots: the version number in draft, published and previous."""
    portal = json.load(request(opener, "/admin/state"))["portal"]
    return {slot: (portal[slot] or {}).get("version") for slot in ("draft", "published", "previous")}


def device_portal(client_address, password, device_ip, path="/portal"):
    """What a Device on the HotSpot gets for a Portal file: (body, Content-Security-Policy)."""
    result = pause_device_fetch(client_address, password, device_ip, path)
    policy = re.search(r"(?:^|,)Content-Security-Policy: ([^,]+)", result.get("http-headers", ""), re.I)
    return result.get("data", ""), policy and policy.group(1).strip()


IMAGE_CAPS = {"logo": 20 * 1024, "cover": 100 * 1024}


def webp(tag: bytes, size: int = 64) -> bytes:
    """size bytes the upload check accepts: the WebP magic, then tag padded. The Controller never decodes them."""
    head = b"WEBP" + tag
    return b"RIFF" + (size - 8).to_bytes(4, "little") + head + b"\0" * (size - 8 - len(head))


def upload_image(opener, name, body):
    """POST raw image bytes to a slot as the admin page does; return (status, text)."""
    response = request(opener, "/admin/portal/images/" + name, "POST", headers={"Content-Type": "image/webp"}, data=body)
    return response.status, response.read().decode()


def portal_images(opener, prefix="/portal/"):
    """What a Portal image path serves for each slot: {name: (bytes, Content-Type)}, Portal headers checked."""
    images = {}
    for name in IMAGE_CAPS:
        response = request(opener, prefix + name)
        assert response.status == 200 and response.headers["X-Content-Type-Options"] == "nosniff", name
        if prefix == "/portal/":
            assert response.headers["Content-Security-Policy"] == PORTAL_CSP, name
        images[name] = (response.read(), response.headers["Content-Type"])
    return images


def custom_portal(opener, report, address, password, client_address, device_ip):
    """An Operator edits, checks, publishes, reverts and resets the Portal; Devices get only what is published."""
    default = portal_files(opener)
    assert "<h1>OpenPiso WiFi</h1>" in default["index.html"] and 'src="/portal/core.js"' in default["index.html"]
    assert default["index.html"] == device_portal(client_address, password, device_ip)[0]
    assert portal_versions(opener) == {"draft": None, "published": None, "previous": None}
    for action in ("publish", "revert"):  # nothing saved yet: nothing to publish or revert to
        assert request(opener, "/admin/portal/" + action, "POST").status == 409, action

    # A preview may try to navigate instead of fetch. Refuse before resolving a Device or reading the Router.
    for path, method in (("state", "GET"), ("coins/open", "POST"), ("coins/done", "POST"),
                         ("redeem", "POST"), ("connect", "POST"), ("recover", "POST"),
                         ("pause", "POST"), ("resume", "POST")):
        for destination in ("document", "iframe", "image", "script"):
            response = request(opener, "/portal/api/" + path, method, headers={"Sec-Fetch-Dest": destination})
            assert response.status == 403 and "fetch requests only" in response.read().decode(), (path, destination)
    for headers in ({}, {"Sec-Fetch-Dest": "empty"}):
        response = request(opener, "/portal/api/state", headers=headers)
        assert response.status != 403 and "rates" in json.load(response), headers

    # The save check: each mistake is refused with what failed, and nothing is stored.
    def edited(name, old, new, count=1):
        assert default[name].count(old) >= count, (name, old)
        return {**default, name: default[name].replace(old, new, count)}

    core = '<script src="/portal/core.js" defer></script>'
    rejected = {
        "missing-marker": (edited("index.html", 'data-action="pause"', ""),
                           'required marker data-action="pause" is missing'),
        "duplicated-marker": (edited("index.html", "</body>", '<p data-portal="remaining"></p></body>'),
                              'required marker data-portal="remaining" appears 2 times; it must appear exactly once'),
        "cross-type-duplicate": (edited("index.html", "</body>", '<form data-portal="remaining"></form></body>'),
                                 'required marker data-portal="remaining" appears 2 times; it must appear exactly once'),
        "cross-type-redeem": (edited("index.html", "</body>", '<div data-action="redeem"></div></body>'),
                              'required marker <form data-action="redeem"> appears 2 times; it must appear exactly once'),
        "redeem-not-form": ({**default, "index.html": re.sub(r'<form data-action="redeem">(.*?)</form>',
                            r'<div data-action="redeem">\1</div>', default["index.html"], count=1, flags=re.S)},
                            'required marker <form data-action="redeem"> must be on a <form>'),
        "external-url": (edited("index.html", "</head>", '<link rel="stylesheet" href="https://cdn.example.com/x.css"></head>'),
                         'index.html: external URL "https://cdn.example.com/x.css"'),
        "w3-image": (edited("index.html", "</body>", '<img src="http://www.w3.org/x.png"></body>'),
                     'index.html: external URL "http://www.w3.org/x.png"'),
        "w3-css-url": ({**default, "portal.css": default["portal.css"] + 'a{background:url(http://www.w3.org/x.png)}'},
                       'portal.css: external URL "http://www.w3.org/x.png"'),
        "w3-css-import": ({**default, "portal.css": default["portal.css"] + '@import "http://www.w3.org/x.css";'},
                          'portal.css: external URL "http://www.w3.org/x.css"'),
        "w3-js": ({**default, "custom.js": 'fetch("http://www.w3.org/x");'},
                  'custom.js: external URL "http://www.w3.org/x"'),
        "protocol-relative-url": ({**default, "portal.css": default["portal.css"] + ".cover{background:url(//cdn.example.com/c.webp)}"},
                                  'portal.css: external URL "//cdn.example.com/c.webp"'),
        "oversized-file": ({**default, "portal.css": default["portal.css"] + "/*" + "x" * 70000 + "*/"},
                           "portal.css is {} bytes; each file must be 64 KB (65536 bytes) or less".format(
                               len(default["portal.css"]) + 70004)),
        "core-js-zero": (edited("index.html", core, ""),
                         'core.js must be loaded exactly once with <script src="/portal/core.js">; found 0'),
        "core-js-two": (edited("index.html", core, core + core),
                        'core.js must be loaded exactly once with <script src="/portal/core.js">; found 2'),
    }
    errors = {}
    for case, (files, error) in rejected.items():
        status, _, failures = save_portal_draft(opener, files)
        assert status == 400 and any(failure.startswith(error) for failure in failures), (case, status, error, failures)
        errors[case] = error
    assert portal_versions(opener) == {"draft": None, "published": None, "previous": None}

    # Uploads: the admin's browser crops and encodes; the Controller checks only the WebP magic
    # bytes and each slot's cap. A refused upload stores nothing.
    builtin = {name: (request(opener, f"/portal/{name}.svg").read(), "image/svg+xml") for name in IMAGE_CAPS}
    assert portal_images(opener) == builtin and portal_images(opener, "/admin/portal/draft/") == builtin
    uploads = {
        "logo-png": ("logo", b"\x89PNG\r\n\x1a\n" + b"\0" * 64, 415),
        "cover-riff-not-webp": ("cover", b"RIFF\x38\0\0\0WAVEfmt " + b"\0" * 52, 415),
        "logo-over-cap": ("logo", webp(b"big", IMAGE_CAPS["logo"] + 1), 413),
        "cover-over-cap": ("cover", webp(b"big", IMAGE_CAPS["cover"] + 1), 413),
    }
    rejected_uploads = {}
    for case, (name, body, expected) in uploads.items():
        status, text = upload_image(opener, name, body)
        assert status == expected, (case, status, text)
        rejected_uploads[case] = f"{status} {text.strip()}"
    assert portal_versions(opener) == {"draft": None, "published": None, "previous": None}
    assert portal_images(opener) == builtin

    # A valid draft saves, but customers keep the Default Portal until it is published. A JS
    # comment is not a protocol-relative URL, an XML namespace is never fetched, and a marker
    # named only in an HTML comment doesn't count.
    first = {**edited("index.html", "<h1>OpenPiso WiFi</h1>", "<h1>Kanto Piso WiFi</h1><!-- data-portal=\"message\" -->"
                      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1 1"></svg>'),
             "portal.css": default["portal.css"] + "\n/* kanto v1 */\n",
             "custom.js": '// kanto v1\n'}
    first["index.html"] = first["index.html"].replace('<aside class="toast"', '<form class="toast"').replace('</aside>', '</form>')
    status, page, failures = save_portal_draft(opener, first)
    assert status == 200 and "Draft saved" in page and not failures, failures
    assert portal_versions(opener) == {"draft": 1, "published": None, "previous": None}
    body, _ = device_portal(client_address, password, device_ip)
    assert "<h1>OpenPiso WiFi</h1>" in body and "Kanto" not in body, "a Device saw the unpublished draft"
    assert "kanto" not in device_portal(client_address, password, device_ip, "/portal/portal.css")[0]
    # A logo at exactly the cap is a new draft carrying the saved files; Devices still get the built-in.
    logo1 = webp(b"logo v1", IMAGE_CAPS["logo"])
    assert upload_image(opener, "logo", logo1)[0] == 200
    assert portal_versions(opener) == {"draft": 2, "published": None, "previous": None}
    assert portal_images(opener, "/admin/portal/draft/") == {"logo": (logo1, "image/webp"), "cover": builtin["cover"]}
    assert portal_images(opener) == builtin, "a Device saw an unpublished image"

    # Publish: Devices get the Custom Portal under the same Portal CSP, with the embedded core.js.
    default_policy = device_portal(client_address, password, device_ip)[1]
    assert request(opener, "/admin/portal/publish", "POST").status == 200
    assert portal_versions(opener) == {"draft": 2, "published": 2, "previous": None}
    first_images = {"logo": (logo1, "image/webp"), "cover": builtin["cover"]}
    assert portal_images(opener) == first_images
    body, policy = device_portal(client_address, password, device_ip)
    assert body == first["index.html"] and policy == default_policy == PORTAL_CSP, policy
    assert device_portal(client_address, password, device_ip, "/portal/portal.css")[0] == first["portal.css"]
    assert device_portal(client_address, password, device_ip, "/portal/custom.js")[0] == first["custom.js"]
    assert device_portal(client_address, password, device_ip, "/portal/core.js")[0] == \
        request(opener, "/portal/core.js").read().decode()

    # A second draft keeps the draft's logo and stays unseen until published, images included;
    # publishing keeps the first as previous.
    second = edited("index.html", "<h1>OpenPiso WiFi</h1>", "<h1>Kanto Piso WiFi 2</h1>")
    assert save_portal_draft(opener, second)[0] == 200
    assert portal_images(opener, "/admin/portal/draft/")["logo"] == (logo1, "image/webp")
    cover2, logo2 = webp(b"cover v2", IMAGE_CAPS["cover"]), webp(b"logo v2")
    assert upload_image(opener, "cover", cover2)[0] == 200 and upload_image(opener, "logo", logo2)[0] == 200
    assert device_portal(client_address, password, device_ip)[0] == first["index.html"]
    assert portal_images(opener) == first_images
    assert request(opener, "/admin/portal/publish", "POST").status == 200
    assert portal_versions(opener) == {"draft": 5, "published": 5, "previous": 2}
    assert device_portal(client_address, password, device_ip)[0] == second["index.html"]
    assert portal_images(opener) == {"logo": (logo2, "image/webp"), "cover": (cover2, "image/webp")}

    # Revert restores the previous version and its images; Reset to default serves the Default
    # Portal with the built-in images.
    assert request(opener, "/admin/portal/revert", "POST").status == 200
    assert portal_versions(opener) == {"draft": 5, "published": 2, "previous": 5}
    assert device_portal(client_address, password, device_ip)[0] == first["index.html"]
    assert portal_images(opener) == first_images
    assert request(opener, "/admin/portal/reset", "POST").status == 200
    assert portal_versions(opener) == {"draft": 5, "published": None, "previous": 2}
    assert device_portal(client_address, password, device_ip)[0] == default["index.html"]
    assert device_portal(client_address, password, device_ip, "/portal/portal.css")[0] == default["portal.css"]
    assert device_portal(client_address, password, device_ip, "/portal/custom.js")[0] == default["custom.js"]
    assert portal_images(opener) == builtin
    # Reset is undone by Revert like any publish, and the editor reopens on the draft.
    assert request(opener, "/admin/portal/revert", "POST").status == 200
    assert device_portal(client_address, password, device_ip)[0] == first["index.html"]
    assert portal_images(opener) == first_images
    assert request(opener, "/admin/portal/reset", "POST").status == 200
    editor = html.unescape(request(opener, "/admin/portal").read().decode())
    assert "Kanto Piso WiFi 2" in editor and "/admin/portal/default/index.html" in editor
    report.snapshot("custom-portal-save-check-publish-revert-reset", address, password, opener,
                    rejected=errors, rejected_uploads=rejected_uploads, versions=portal_versions(opener), same_csp=True,
                    api_navigation_refused=True, fetch_and_headerless_clients_allowed=True)


def sales_report(opener, report, address, password, client_address):
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    before = sales(opener)
    assert "rows" in before, "Sales JSON must include paginated rows"
    default_page = request(opener, "/admin/sales").read().decode()
    assert 'data-view="table"' in default_page
    for mode in ("table", "feed"):
        assert json.load(request(opener, "/admin/sales?format=json&from=1970-01-01&to=2100-01-01&view=" + mode)) == before
        document = request(opener, "/admin/sales?view=" + mode + "&page_size=50&page=2&q=needle").read().decode()
        assert f'data-view="{mode}"' in document
        assert f'name="view" value="{mode}"' in document
        for url, label in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', document):
            url = html.unescape(url)
            if url.startswith("/admin/sales?"):
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                assert params["view"] == [label.lower() if label in ("Table", "Feed") else mode]
                assert params["q"] == ["needle"] and params["page_size"] == ["50"]
    for invalid in ("grid", "TABLE", "Feed", "a"):
        assert request(opener, "/admin/sales?view=" + invalid).status == 400
    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    assert vendo.heartbeat()[0] == 200
    state = json.load(request(opener, "/admin/state"))
    station = next(v for v in state["vendos"] if v["id"] == vendo.id)
    assert station["name"] == "", "Router hostnames must not be saved automatically"
    leases = router_rest(address, "admin", password, "/ip/dhcp-server/lease")
    hostname = next((lease.get("host-name", "") for lease in leases if lease.get("mac-address", "").upper() == station["mac"].upper()), "")
    vendos_page = request(opener, "/admin/vendos").read().decode()
    name_form = next(form for form in re.findall(r'<form\b.*?</form>', vendos_page, re.S)
                     if 'action="/admin/vendos/edit"' in form and f'name="id" value="{vendo.id}"' in form)
    assert html.unescape(re.search(r'name="name"[^>]*value="([^"]*)"', name_form).group(1)) == hostname
    assert next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)["name"] == ""
    assert request(opener, "/admin/vendos/edit", "POST", {"id": vendo.id, "name": "  Zone 12  ", "location": ""}).status == 200
    assert next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)["name"] == "Zone 12"
    saved_page = request(opener, "/admin/vendos").read().decode()
    assert 'name="name" value="Zone 12"' in saved_page
    rejected_name = request(opener, "/admin/vendos/edit", "POST", {"id": vendo.id, "name": "X" * 81, "location": ""})
    assert rejected_name.status == 400 and "80 characters" in rejected_name.read().decode()
    assert request(opener, "/admin/vendos/edit", "POST", {"id": "0", "name": "Zone", "location": ""}).status == 400
    assert request(opener, "/admin/vendos/edit", "POST", {"id": "4294967295", "name": "Zone", "location": ""}).status == 404
    assert request(urllib.request.build_opener(), "/admin/vendos/edit", "POST", {"id": vendo.id, "name": "Zone", "location": ""}).status == 401
    # A receipt with no Window contributes to Total Sales, not a row.
    vendo.total += 3
    assert vendo.post(vendo.frame())[0] == 200
    for _ in range(26):
        opened = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id}))
        window_id = opened["coin_window"]["id"]
        assert vendo.heartbeat()[0] == 200
        vendo.insert(1)
        assert vendo.heartbeat()[0] == 200
        current = sales(opener)
        assert not any(row["id"] == str(window_id) and row["type"] == "coin" for row in current["rows"])
        pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": window_id})
        assert vendo.heartbeat()[0] == 200
        current = sales(opener)
        assert not any(row["id"] == str(window_id) and row["type"] == "coin" for row in current["rows"])
        assert vendo.heartbeat()[0] == 200
    first = sales(opener)["rows"][0]
    assert first["type"] == "coin" and first["amount"] == 1
    assert first["vendo_name"] == "Zone 12"
    named_page = request(opener, "/admin/sales").read().decode()
    assert f'<option value="{vendo.id}" >Zone 12</option>' in named_page
    assert '<td>Zone 12</td>' in named_page
    ledger = json.load(request(opener, "/admin/state"))
    validity = next(v for v in ledger["validity"] if v["time_credit_id"] == first["user"])
    assert datetime.datetime.fromisoformat(first["validity"]).timestamp() == validity["valid_until"]
    time.sleep(1.1)  # Validity uses whole seconds; make the late Top-up observable.
    vendo.total += 1
    vendo.rseq += 1
    late_report = _vendo_module.sign(1, (vendo.id, vendo.epoch, vendo.total, window_id, vendo.nonce, vendo.rseq), vendo.key)
    assert vendo.post(late_report)[0] == 200
    late = sales(opener)["rows"][0]
    assert late["id"] == first["id"] and late["at"] == first["at"]
    assert late["amount"] == 2 and late["credited_seconds"] > first["credited_seconds"]
    ledger = json.load(request(opener, "/admin/state"))
    window = next(w for w in ledger["coin_windows"] if str(w["id"]) == late["id"])
    assert (late["user"], late["ip"], late["mac"], late["credited_seconds"]) == (window["time_credit_id"], window["ip"], window["mac"], window["credited_seconds"])
    validity = next(v for v in ledger["validity"] if v["time_credit_id"] == late["user"])
    assert datetime.datetime.fromisoformat(late["validity"]).timestamp() == validity["valid_until"]
    assert datetime.datetime.fromisoformat(late["validity"]) > datetime.datetime.fromisoformat(first["validity"])
    voucher_rows = []
    for price in ("7", "0"):
        time.sleep(1.1)  # Later purchases must not rewrite earlier sale snapshots.
        form = {"seconds": "60", "count": "1", "pesos": price}
        code = voucher_codes(opener, generate_vouchers(opener, form))[0]
        redeemed = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}))
        row = sales(opener)["rows"][0]
        assert row["type"] == "voucher" and row["user"] == code
        assert row["amount"] == int(price) and row["credited_seconds"] == 60
        assert (row["ip"], row["mac"].upper()) == (device_ip, _lab_module.DEVICE_MAC.upper())
        assert datetime.datetime.fromisoformat(row["validity"]).timestamp() == redeemed["time_credit"]["valid_until"]
        voucher_rows.append(row)
    opened = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id}))
    assert vendo.heartbeat()[0] == 200
    vendo.insert(2)
    assert vendo.heartbeat()[0] == 200
    filtered_path = f"/admin/sales?from=1970-01-01&to=2099-12-31&vendo={vendo.id}&page_size=25&view=feed"
    filtered = json.load(request(opener, filtered_path + "&format=json"))
    assert filtered["count"] == 26 and filtered["total_pesos"] == filtered["coin_pesos"] == 32
    assert filtered["voucher_pesos"] == filtered["redeemed_vouchers"] == filtered["unpriced_vouchers"] == 0
    assert all(row["type"] == "coin" and row["vendo_id"] == vendo.id for row in filtered["rows"])
    assert json.load(request(opener, filtered_path + "&format=json&page=2"))["rows"][0]["vendo_id"] == vendo.id
    all_vendos = json.load(request(opener, "/admin/sales?format=json&from=1970-01-01&to=2100-01-01&vendo=all"))
    assert all_vendos == sales(opener)
    for invalid in ("4294967295", "0", "-1", "not-a-vendo"):
        assert request(opener, "/admin/sales?vendo=" + invalid).status == 400
    full = request(opener, filtered_path).read().decode()
    fragment = request(opener, filtered_path, headers={"HX-Request": "true"}).read().decode()
    restored = request(opener, filtered_path, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}).read().decode()
    assert "<!doctype html>" in full and "<!doctype html>" in restored and "<!doctype html>" not in fragment
    for document in (full, restored):
        assert f'<option value="{vendo.id}" selected>' in document
        assert 'name="from" value="1970-01-01"' in document and 'name="to" value="2099-12-31"' in document
        assert '<span>Total Sales</span><strong>₱32</strong>' in document
    assert 'id="sales-report"' in fragment and 'hx-target="#sales-report"' in fragment
    assert 'hx-push-url="true"' in fragment and 'hx-trigger="submit, change"' in fragment
    form = re.search(r"<form\b.*?</form>", fragment, re.S).group(0)
    assert 'name="page"' not in form, "filter changes must reset the page"
    local_today = datetime.date.fromisoformat(json.load(request(opener, "/admin/sales?format=json"))["to"])
    starts = {
        "Today": local_today,
        "This week": local_today - datetime.timedelta(days=local_today.weekday()),
        "This month": local_today.replace(day=1),
        "This year": local_today.replace(month=1, day=1),
    }
    dashboard_totals = dashboard(opener)["sales"]
    shortcut_evidence = {}
    all_page = request(opener, "/admin/sales?page_size=50&page=2").read().decode()
    for label, key in (("Today", "today"), ("This week", "week"), ("This month", "month"), ("This year", "year")):
        shortcut_url = html.unescape(re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>' + label + r'</a>', all_page).group(1))
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(shortcut_url).query)
        assert params["from"] == [starts[label].isoformat()] and params["to"] == [local_today.isoformat()]
        assert params["page_size"] == ["50"] and "page" not in params
        shortcut = json.load(request(opener, shortcut_url + "&format=json"))
        assert shortcut["page"] == 1 and shortcut["total_pesos"] == dashboard_totals[key]
        assert "<!doctype html>" in request(opener, shortcut_url).read().decode()
        per_vendo_url = html.unescape(re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>' + label + r'</a>', fragment).group(1))
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(per_vendo_url).query)["vendo"] == [str(vendo.id)]
        shortcut_evidence[key] = shortcut
    pager = re.search(r'<nav aria-label="Sales pages">.*?</nav>', fragment, re.S).group(0)
    pager_urls = [html.unescape(url) for url in re.findall(r'href="([^"]+)"', pager)]
    assert pager_urls
    for url in pager_urls:
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert params["vendo"] == [str(vendo.id)] and params["page_size"] == ["25"]
        assert params["view"] == ["feed"]
        assert params["from"] == ["1970-01-01"] and params["to"] == ["2099-12-31"]
    search_evidence = {}
    search_base = "/admin/sales?from=1970-01-01&to=2099-12-31&page_size=100"
    for label, query in (("ip", device_ip[1:]),
                         ("mac-colon", late["mac"].lower()),
                         ("mac-hyphen", late["mac"].upper().replace(":", "-")),
                         ("mac-substring", late["mac"][3:-3].lower().replace(":", "-")),
                         ("time-credit", late["user"][1:-1].upper())):
        path = search_base + "&vendo=" + str(vendo.id) + "&q=" + urllib.parse.quote(query, safe="")
        matched = json.load(request(opener, path + "&format=json"))
        assert matched["count"] == len(matched["rows"]) == 26, (label, matched)
        assert matched["total_pesos"] == matched["coin_pesos"] == 32
        assert all(row["type"] == "coin" and row["vendo_id"] == vendo.id for row in matched["rows"])
        search_evidence[label] = matched
    for voucher_row in voucher_rows:
        query = voucher_row["user"][1:-1].lower()
        matched = json.load(request(opener, search_base + "&format=json&q=" + urllib.parse.quote(query, safe="")))
        assert matched["count"] == 1 and matched["rows"] == [voucher_row]
        assert matched["total_pesos"] == all_vendos["total_pesos"]
        search_evidence["voucher-" + voucher_row["user"]] = matched
    for query in (device_ip, late["mac"].lower().replace(":", "-")):
        matched = json.load(request(opener, search_base + "&format=json&q=" + urllib.parse.quote(query, safe="")))
        assert matched["count"] >= 28 and matched["total_pesos"] == all_vendos["total_pesos"]
        for voucher_row in voucher_rows:
            assert voucher_row in matched["rows"], "Search must use Voucher sale-time IP/MAC"
    # Time Credit ids are base64url, so "_" can legitimately appear: wildcards
    # must match exactly the rows that contain them literally.
    vendo_rows = json.load(request(opener, search_base + "&vendo=" + str(vendo.id) + "&format=json"))["rows"]
    for query in ("no-such-customer", "%", "_", "' OR 1=1 --"):
        literal = [row for row in vendo_rows if query.lower() in (row["ip"] + " " + row["mac"] + " " + row["user"]).lower()]
        matched = json.load(request(opener, search_base + "&vendo=" + str(vendo.id) + "&format=json&q=" + urllib.parse.quote(query, safe="")))
        assert matched["count"] == len(literal) and matched["rows"] == literal, (query, matched["count"], len(literal))
        assert matched["total_pesos"] == 32, "Search must not filter receipt totals"
    assert not [row for row in vendo_rows if "%" in row["user"]] and json.load(request(opener, filtered_path + "&format=json&q=%25"))["page"] == 1
    empty_search = json.load(request(opener, filtered_path + "&format=json&q="))
    assert empty_search == filtered
    outside_period = json.load(request(opener, "/admin/sales?format=json&from=1970-01-01&to=1970-01-01&q=" + device_ip))
    assert outside_period["count"] == 0 and outside_period["total_pesos"] == 0
    search_path = filtered_path + "&page=2&q=" + urllib.parse.quote(late["user"].upper(), safe="")
    for headers in ({}, {"HX-Request": "true"}, {"HX-Request": "true", "HX-History-Restore-Request": "true"}):
        document = request(opener, search_path, headers=headers).read().decode()
        search_form = re.search(r"<form class=\"sales-filters\".*?</form>", document, re.S).group(0)
        assert f'name="q" value="{late["user"].upper()}"' in search_form
        assert 'name="page"' not in search_form, "Changing search must submit page 1"
        assert 'hx-push-url="true"' in search_form
        assert "Showing 26 to 26 of 26 entries" in document
        for url in re.findall(r'href="([^"]+)"', document):
            if not html.unescape(url).startswith("/admin/sales?"):
                continue
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(html.unescape(url)).query)
            assert params["q"] == [late["user"].upper()]
            assert params["vendo"] == [str(vendo.id)] and params["page_size"] == ["25"]
        reset_query = urllib.parse.parse_qs(urllib.parse.urlsplit(search_path).query)
        reset_query.pop("page")
        reset_query["format"] = ["json"]
        reset = json.load(request(opener, "/admin/sales?" + urllib.parse.urlencode(reset_query, doseq=True)))
        assert reset["page"] == 1 and reset["count"] == 26
    report.snapshot("sales-report-search-ip-mac-user-literal-totals-navigation", address, password, opener,
                    searches=search_evidence)
    result = sales(opener)
    assert next(row for row in result["rows"] if row["type"] == "coin" and row["id"] == late["id"]) == late
    for voucher_row in voucher_rows:
        assert next(row for row in result["rows"] if row["type"] == "voucher" and row["id"] == voucher_row["id"]) == voucher_row
    assert result["count"] == before["count"] + 28
    assert result["total_pesos"] - before["total_pesos"] == 39
    assert result["page"] == 1 and result["page_size"] == 25 and len(result["rows"]) == 25
    for size in (25, 50, 100):
        page = json.load(request(opener, f"/admin/sales?format=json&from=1970-01-01&to=2099-12-31&page_size={size}&page=999999"))
        assert page["page"] == max(1, (page["count"] + size - 1) // size)
        assert len(page["rows"]) <= size
    for query in ("page_size=1", "page_size=101", "page_size=no", "page=0", "page=-1", "page=no"):
        assert request(opener, "/admin/sales?" + query).status == 400
    for path in ("/admin/sales", "/admin/sales?format=json"):
        assert_login_redirect(path)
    empty = sales(opener, "1970-01-01", "1970-01-01")
    assert empty["rows"] == [] and empty["count"] == 0 and empty["page"] == 1
    today = datetime.date.today().isoformat()
    assert dashboard(opener)["sales"]["today"] == sales(opener, today, today)["total_pesos"]
    page = request(opener, "/admin/sales").read().decode()
    assert "Showing 1 to 25 of" in page and "Validity" in page
    assert not contains_secret_key(result)
    report.snapshot("sales-report-rows-late-stray-open-vouchers-pagination-auth", address, password, opener,
                    sales=result, dashboard=dashboard_totals, shortcuts=shortcut_evidence)
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": opened["coin_window"]["id"]})
    assert vendo.heartbeat()[0] == 200
    assert vendo.heartbeat()[0] == 200
    assert request(opener, "/admin/vendos/unpair", "POST", {"id": vendo.id}).status == 204
    historical = json.load(request(opener, filtered_path + "&format=json"))
    assert historical["total_pesos"] == 32 and historical["count"] == 27
    assert all(row["vendo_name"] == "Zone 12 (unpaired)" for row in historical["rows"])
    paired_second = pair_board(opener, client_address, password, coin_board(client_address, password))
    second = SimulatedVendo(BASE_URL, paired_second["id"], bytes.fromhex(paired_second["secret"]), timeout=HTTP_TIMEOUT)
    assert second.heartbeat()[0] == 200
    opened_second = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": second.id}))
    assert second.heartbeat()[0] == 200
    second.insert(5)
    assert second.heartbeat()[0] == 200
    code = voucher_codes(opener, generate_vouchers(opener, {"seconds": "60", "count": "1"}))[0]
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": opened_second["coin_window"]["id"]})
    assert second.heartbeat()[0] == 200
    # Close near the start of a second so a subsequent redemption exercises
    # newest-first ordering without the Validity checks' deliberate delays.
    time.sleep(1 - time.time() % 1)
    assert second.heartbeat()[0] == 200
    redeemed = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}))
    assert_message(redeemed, "success", "online")
    newest = sales(opener)["rows"][:2]
    assert newest[0]["type"] == "voucher" and newest[0]["user"] == code, newest
    assert newest[1]["type"] == "coin" and newest[1]["id"] == str(opened_second["coin_window"]["id"]), newest
    second_sales = json.load(request(opener, f"/admin/sales?format=json&from=1970-01-01&to=2099-12-31&vendo={second.id}"))
    assert second_sales["count"] == 1 and second_sales["total_pesos"] == second_sales["coin_pesos"] == 5
    assert second_sales["redeemed_vouchers"] == 0
    assert all(row["type"] == "coin" and row["vendo_id"] == second.id for row in second_sales["rows"])
    assert second_sales["rows"][0]["vendo_name"] == f"Vendo {second.id}"
    unnamed_page = request(opener, f"/admin/sales?vendo={second.id}").read().decode()
    assert f'<td>Vendo {second.id}</td>' in unnamed_page
    assert request(opener, "/admin/vendos/edit", "POST", {"id": second.id, "name": "Temporary", "location": ""}).status == 200
    assert request(opener, "/admin/vendos/edit", "POST", {"id": second.id, "name": " \t ", "location": ""}).status == 200
    assert next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == second.id)["name"] == ""
    selector = request(opener, filtered_path).read().decode()
    options = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', re.search(r'<select name="vendo"[^>]*>(.*?)</select>', selector, re.S).group(1))
    assert next(label for value, label in options if value == str(vendo.id)) == "Zone 12 (unpaired)"
    assert next(label for value, label in options if value == str(second.id)) == f"Vendo {second.id}"
    assert [value for value, _ in options].index(str(second.id)) < [value for value, _ in options].index(str(vendo.id))
    labels = [label for _, label in options]
    first_unpaired = next(index for index, label in enumerate(labels) if label.endswith("(unpaired)"))
    assert all(label.endswith("(unpaired)") for label in labels[first_unpaired:])
    report.snapshot("sales-report-vendo-filter-unpaired-history", address, password, opener,
                    filtered=filtered, historical=historical, second=second_sales, newest=newest)
    # Export is a plain attachment of the whole filtered report, not its page.
    export_base = "/admin/sales/export?from=1970-01-01&to=2099-12-31"
    for path in ("/admin/sales/export", export_base):
        assert_login_redirect(path)
    for query in ("from=bad", "from=2099-01-01&to=1970-01-01", "vendo=4294967295", "page_size=1", "page=0"):
        assert request(opener, "/admin/sales/export?" + query).status == 400
    headers = ["#", "Date", "Time", "IP", "MAC", "User", "Validity", "Type", "Vendo", "seller", "Credited time (seconds)", "Amount (PHP)"]
    export_evidence = []
    for query in ("", "&vendo=" + str(vendo.id), "&q=" + urllib.parse.quote(late["user"], safe=""),
                  "&q=" + urllib.parse.quote(voucher_rows[1]["user"], safe=""), "&q=no-such-sale"):
        expected = json.load(request(opener, search_base + "&format=json" + query))
        download = request(opener, export_base + query + "&page=2&page_size=25", headers={"HX-Request": "true"})
        assert download.status == 200, download.status
        assert download.headers["Content-Type"] == "text/csv; charset=utf-8"
        assert download.headers["Cache-Control"] == "no-store"
        assert "attachment" in download.headers["Content-Disposition"]
        assert "sales-1970-01-01-to-2099-12-31.csv" in download.headers["Content-Disposition"]
        body = download.read()
        assert body.startswith(b"\xef\xbb\xbf")
        text = body.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        assert next(csv.reader(io.StringIO(text))) == headers
        assert len(rows) == expected["count"], (len(rows), expected["count"])
        for number, (row, sale) in enumerate(zip(rows, expected["rows"]), 1):
            assert row["#"] == str(number)
            at = datetime.datetime.fromisoformat(sale["at"])
            assert row["Date"] == at.strftime("%Y-%m-%d") and row["Time"] == at.strftime("%H:%M:%S")
            assert row["User"] == ("'" + sale["user"] if sale["user"].startswith(("=", "+", "-", "@")) else sale["user"])
            assert row["IP"] == sale["ip"] and row["MAC"] == sale["mac"]
            assert row["Validity"] == sale["validity"] and row["Vendo"] == sale["vendo_name"]
            assert row["Credited time (seconds)"] == str(sale["credited_seconds"])
            assert row["Amount (PHP)"] == (str(sale["amount"]) if sale["amount"] is not None else "")
            assert row["Type"] == ("Coin" if sale["type"] == "coin" else "Voucher")
        assert redeemed["time_credit"]["resume_code"] not in text
        assert not any(key in text.lower() for key in ("resume_code", "router_password", "token", "credential"))
        export_evidence.append({"query": query, "rows": len(rows), "bom": True})
    export_page = request(opener, search_path).read().decode()
    link = re.search(r'<a\b[^>]*class="[^"]*\bsales-export\b[^"]*"[^>]*>', export_page).group(0)
    assert 'hx-boost="false"' in link and "hx-get" not in link
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(html.unescape(re.search(r'href="([^"]+)"', link).group(1))).query)
    assert params["q"] == [late["user"].upper()] and params["vendo"] == [str(vendo.id)]
    assert params["from"] == ["1970-01-01"] and params["to"] == ["2099-12-31"] and "page" not in params
    for name in ('=1+1,"₱"\nZone', "+Zone", "-Zone", "@Zone", "\t=Zone", "\r=Zone"):
        assert request(opener, "/admin/vendos/edit", "POST", {"id": second.id, "name": name, "location": ""}).status == 200
        body = request(opener, export_base + "&vendo=" + str(second.id)).read().decode("utf-8-sig")
        row = next(csv.DictReader(io.StringIO(body)))
        assert row["Vendo"] == "'" + name.strip(), row
    report.snapshot("sales-report-csv-export", address, password, opener, exports=export_evidence)
    assert request(opener, "/admin/vendos/unpair", "POST", {"id": second.id}).status == 204


def duration_seconds(value):
    parts = re.findall(r"(\d+)([wdhms])", value)
    assert "".join(number + unit for number, unit in parts) == value, value
    return sum(int(number) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit] for number, unit in parts)


def contains_secret_key(value) -> bool:
    if isinstance(value, dict):
        return any("password" in key.lower() or contains_secret_key(child) for key, child in value.items())
    if isinstance(value, list):
        return any(contains_secret_key(child) for child in value)
    return False


def redact_resume_codes(value):
    if isinstance(value, str):
        return re.sub(r'(<code class="resume-code">)[A-HJ-NP-Z2-9]{8}(</code>)', r'\1[redacted]\2', value)
    if isinstance(value, dict):
        return {key: "[redacted]" if key == "resume_code" and child else redact_resume_codes(child) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_resume_codes(child) for child in value]
    return value


class Report:
    def __init__(self, binary: str, path: Path):
        self.path = path
        self.lab_snapshot = None
        self.data = {
            "format_version": 1,
            "scenario": "6-skeleton-7-vouchers-8-pause-9-top-up-10-rebind-11-reconciler-13-admin-dashboard-15-vendo-16-coin-window-41-vlan-auto-select-61-default-portal-62-custom-portal",
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "git_tree": subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "git_dirty": bool(subprocess.run(
                ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True,
            ).stdout.strip()),
            "binary_size_bytes": Path(binary).stat().st_size,
            "binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
            "test_seam": "Controller binary via external HTTP against chr-lab and chr-client",
            "steps": [],
            "result": "running",
        }

    def capture_lab_failure(self) -> None:
        # Nested cleanup paths call this before changing the failing lab state.
        # Keep the first capture, and let the original exception keep unwinding.
        if sys.exception() is None or "lab_snapshot" in self.data:
            return
        if self.lab_snapshot is None:
            return
        try:
            self.data["lab_snapshot"] = self.lab_snapshot()
        except Exception as error:
            self.data["lab_snapshot"] = {"error": f"{type(error).__name__}: {error}"}

    def snapshot(self, name: str, address: str, password: str, opener=None, **evidence) -> None:
        ledger = None
        if opener is not None:
            response = request(opener, "/admin/state")
            assert response.status == 200
            ledger = json.load(response)
            assert not contains_secret_key(ledger)
        router = capture_baseline(address, password)
        router["active"] = router_rest(address, "admin", password, "/ip/hotspot/active")
        router["hosts"] = router_rest(address, "admin", password, "/ip/hotspot/host")
        router["hotspot_users"] = [
            {key: entry.get(key, "") for key in ("name", "profile", "limit-uptime", "uptime", "disabled", "mac-address")}
            for entry in router_rest(address, "admin", password, "/ip/hotspot/user")
        ]
        step = {"name": name, "ledger": ledger, "router": router, "evidence": evidence}
        self.data["steps"].append(step)
        print(f"PASS {name}", flush=True)

    def finish(self, result: str, error: str = "") -> None:
        self.data["result"] = result
        self.data["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if error:
            self.data["error"] = error
        summary = {key: value for key, value in self.data.items() if key not in ("steps", "vlan_hosts", "lab_snapshot", "chr_client_cleanup")}
        summary["steps"] = [step["name"] for step in self.data["steps"]]
        if "chr_client_cleanup" in self.data:
            summary["chr_client_cleanup_removed"] = len(self.data["chr_client_cleanup"].get("removed", []))
        if "execution" in summary:  # the serial tail is a raw log; keep it in the local report only
            summary["execution"] = {key: value for key, value in summary["execution"].items() if key != "serial_console_tail"}
        lab = load_lab_env()

        def encode(data):
            encoded = json.dumps(redact_resume_codes(data), indent=2, ensure_ascii=False)
            for secret in (lab["CHR_PASSWORD"], ROUTER_ADMIN_PASSWORD, ADMIN_PASSWORD):
                encoded = encoded.replace(secret, "[redacted]")
            return encoded + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(encode(self.data))
        summary_path = self.path.with_suffix(".summary.json")
        summary_path.write_text(encode(summary))
        print(f"Report: {self.path}", flush=True)
        print(f"Summary: {summary_path}", flush=True)


def coin_window_empty(opener, report, address, password, client_address):
    # Use the second unentitled Device so #13 can verify #15's exact sales first.
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan2", scheme="http")[0]["address"].split("/")[0]
    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    opened = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]}))
    assert_message(opened, "success", "coins-insert")
    assert opened["time_credit"]["bought"] == 0
    state = json.load(request(opener, "/admin/state"))
    window = state["coin_windows"][-1]
    assert opened["coin_window"]["id"] == window["id"] and opened["coin_window"]["vendo_id"] == paired["id"]
    tc = next(tc for tc in state["purchases"]["time_credits"] if tc["id"] == window["time_credit_id"])
    assert tc["bought_seconds"] == 0
    assert not any(i["time_credit_id"] == tc["id"] for i in state["credit_intents"])
    connected = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/connect", {}))
    assert connected["message"] is None or connected["message"]["code"] != "online"
    assert not connected["time_credit"]["online"]
    assert not router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    assert vendo.heartbeat()[0] == 200
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": window["id"]})
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    assert vendo.heartbeat()[0] == 200
    state = json.load(request(opener, "/admin/state"))
    assert not any(a["time_credit_id"] == tc["id"] for a in state["purchases"]["audit"])
    assert not any(i["time_credit_id"] == tc["id"] for i in state["credit_intents"])
    assert state["coin_windows"][-1]["closed_at"]
    assert request(opener, "/admin/vendos/unpair", "POST", {"id": vendo.id}).status == 204
    report.snapshot("empty-coin-window-creates-identity-without-free-router-time", address, password, opener,
                    zero_bought_seconds=True, zero_router_users=True)


def coin_window_open(opener, report, address, password, client_address, device_ip):
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=10\n5=60"}).status == 200
    for invalid in ("5=60", "1=10\n1=20", "1=10\n5=40", "1=-1"):
        assert request(opener, "/admin/rates", "POST", {"tiers": invalid}).status == 400
    assert 'data-cost="5" data-seconds="60"' in request(opener, "/admin/rates").read().decode()
    state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state"))
    assert [(r["pesos"], r["seconds"]) for r in state["rates"]] == [(1, 10), (5, 60)]
    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    barrier = threading.Barrier(2)
    def insert(_):
        barrier.wait()
        try:
            result = pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]})
            assert portal_state(result)["coin_window"]["status"] == "open"
            return "opened"
        except urllib.error.HTTPError as error:
            assert "409" in json.load(error).get("detail", "")
            return "busy"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(insert, range(2))) == ["busy", "opened"]
    state = json.load(request(opener, "/admin/state"))
    windows = [w for w in state["coin_windows"] if w["vendo_id"] == paired["id"]]
    assert len(windows) == 1 and windows[0]["status"] == "open"
    second_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan2", scheme="http")[0]["address"].split("/")[0]
    try:
        pause_device_fetch(client_address, password, second_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]})
    except urllib.error.HTTPError as error:
        assert "409" in json.load(error).get("detail", "")
    else:
        raise AssertionError("second customer reserved a busy Vendo")
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    status, body = vendo.heartbeat()
    assert status == 200
    frames = _vendo_module.decode_frames(body, vendo.key)
    assert [tag for tag, _ in frames] == [3, 2]
    assert 580000 < frames[1][1][2] <= 600000
    assert vendo.window_id == windows[0]["id"]
    first_cap = vendo.expires_at
    time.sleep(0.05)
    assert vendo.heartbeat()[0] == 200 and vendo.expires_at <= first_cap + 0.1
    stale = _vendo_module.sign(1, (vendo.id, vendo.epoch, vendo.total, vendo.window_id, vendo.nonce, vendo.rseq - 1), vendo.key)
    status, body = vendo.post(stale)
    assert status == 200
    frames = _vendo_module.decode_frames(body, vendo.key)
    assert frames[0][1][-1] == vendo.rseq - 1 and frames[1][1][-1] == vendo.rseq
    report.snapshot("coin-window-rate-table-and-unique-vendo-reservation", address, password, opener,
                    simultaneous_insert_exactly_one=True, second_customer_busy=True)
    return paired, vendo, windows[0]


def coin_window_done(opener, report, address, password, client_address, device_ip, vendo, window):
    sales_before = sales(opener)["coin_pesos"]
    before = json.load(request(opener, "/admin/state"))
    tc = next(tc for tc in before["purchases"]["time_credits"] if tc["id"] == window["time_credit_id"])
    vendo.insert(4)
    repeated = vendo.frame()
    assert vendo.post(repeated, ignore_ack=True)[0] == 200
    assert vendo.post(repeated)[0] == 200
    live = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state"))["coin_window"]
    assert live["id"] == window["id"] and live["coins"] == 4 and live["status"] == "open"
    assert time.time() < live["closes_at"] <= time.time() + 61, live
    # A fifth pulse was accepted before Done, but its burst has not reported yet.
    vendo.insert(1)
    closing = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": window["id"]}))
    assert_message(closing, "success", "coins-closing")
    assert closing["coin_window"]["status"] == "closing" and closing["coin_window"]["closes_at"] == 0
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    try:
        vendo.insert(1)
    except ValueError:
        pass
    else:
        raise AssertionError("close did not inhibit the acceptor")
    closed = vendo.frame(5)
    time.sleep(0.3)
    assert vendo.post(closed, ignore_ack=True)[0] == 200
    assert vendo.post(closed)[0] == 200
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        after = json.load(request(opener, "/admin/state"))
        intent = next(i for i in after["credit_intents"] if i["time_credit_id"] == tc["id"])
        if not intent["pending"]:
            break
        time.sleep(0.2)
    updated = next(item for item in after["purchases"]["time_credits"] if item["id"] == tc["id"])
    assert updated["bought_seconds"] == tc["bought_seconds"] + 60
    credits = [a for a in after["purchases"]["audit"] if a["reason"] == "coin-top-up" and a["source"].startswith(f"coin-window:{window['id']}:")]
    assert len(credits) == 1 and credits[0]["after_seconds"] - credits[0]["before_seconds"] == 60
    assert not intent["pending"]
    user = router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + tc["id"])[0]
    assert duration_seconds(user["limit-uptime"]) == updated["bought_seconds"]
    assert router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
    sales_after = sales(opener)["coin_pesos"]
    assert sales_after == sales_before + 5
    internet_target, internet = device_internet(client_address, password, device_ip)
    assert "/portal" not in internet
    report.snapshot("coin-done-in-flight-tiered-top-up-and-replayed-closed", address, password, opener,
                    live_coins_before_done=4, final_coins=5, bought_seconds_added=60,
                    coin_sales_added=sales_after - sales_before, internet_reachable=True,
                    internet_target=internet_target)


def coin_window_wait(opener, window_id, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = json.load(request(opener, "/admin/state"))
        window = next(w for w in state["coin_windows"] if w["id"] == window_id)
        if predicate(window):
            return state, window
        time.sleep(0.2)
    raise AssertionError(f"Coin Window {window_id} did not reach expected state: {window}")


def coin_window_grace(opener, report, address, password, client_address, device_ip, vendo):
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id})
    assert vendo.heartbeat()[0] == 200
    window_id = vendo.window_id
    vendo.insert(4)
    assert vendo.heartbeat()[0] == 200
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": window_id})
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    state, window = coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
    assert window["coins"] == 4 and window["credited_seconds"] == 40 and window["closed_at"] == 0
    try:
        pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id})
    except urllib.error.HTTPError as error:
        assert "409" in json.load(error).get("detail", "")
    else:
        raise AssertionError("missing Closed released a Vendo")
    # Direct assignment represents a pulse accepted before Close but delivered late.
    vendo.total += 1
    late = vendo.frame(5)
    assert vendo.post(late, ignore_ack=True)[0] == 200
    assert vendo.post(late)[0] == 200
    state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    assert window["coins"] == 5 and window["credited_seconds"] == 60
    credits = [a for a in state["purchases"]["audit"] if a["source"].startswith(f"coin-window:{window_id}:")]
    assert [a["after_seconds"] - a["before_seconds"] for a in credits] == [40, 20]
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=1"}).status == 200
    late_report = _vendo_module.sign(1, (vendo.id, vendo.epoch, vendo.total + 1, window_id, vendo.nonce, vendo.rseq + 1), vendo.key)
    assert vendo.post(late_report)[0] == 200
    assert vendo.post(late_report)[0] == 200
    state, window = coin_window_wait(opener, window_id, lambda w: w["coins"] == 6)
    assert window["credited_seconds"] == 72
    sales = next(row for row in state["coin_sales"] if row["vendo_id"] == vendo.id)
    assert sales["coins"] == 11 and sales["credited_seconds"] == 132
    assert "11" in request(opener, "/admin/coins").read().decode()
    assert state["reconciliation"]["coin_gaps"] == []
    vendo.total += 1
    vendo.rseq += 1
    report.snapshot("coin-grace-holds-busy-and-late-coins-reprice-whole-window", address, password, opener,
                    grace_credit=40, late_tier_credit=20, later_credit=12, rates_snapshot_preserved=True)


def coin_window_boot(controller, args, data_dir, opener, report, address, password, client_address, device_ip, vendo):
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id})
    assert vendo.heartbeat()[0] == 200
    window_id = vendo.window_id
    vendo.insert(1)
    assert vendo.heartbeat()[0] == 200
    controller.kill()
    controller = Controller(args.binary, data_dir, initialize=False)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
        state, window = coin_window_wait(opener, window_id, lambda w: w["status"] != "open")
        assert window["close_at"] and window["closed_at"] == 0
        assert vendo.heartbeat()[0] == 200 and vendo.closing
        assert vendo.heartbeat()[0] == 200
        state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
        assert window["credited_seconds"] == 1
        credits = [a for a in state["purchases"]["audit"] if a["source"].startswith(f"coin-window:{window_id}:")]
        assert len(credits) == 1
        report.snapshot("coin-window-open-at-restart-closes-on-boot", address, password, opener,
                        credited_once=True, final_coins=1)
        return controller, opener
    except BaseException:
        report.capture_lab_failure()
        controller.stop()
        raise


def coin_window_idle(opener, report, address, password, client_address, device_ip, vendo):
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id})
    assert vendo.heartbeat()[0] == 200
    window_id = vendo.window_id
    vendo.insert(1)
    assert vendo.heartbeat()[0] == 200
    started = time.monotonic()
    deadline = started + 75
    while time.monotonic() < deadline:
        assert vendo.heartbeat()[0] == 200
        if vendo.closing:
            break
        time.sleep(1)
    assert vendo.closing and 55 <= time.monotonic() - started <= 75
    assert vendo.heartbeat()[0] == 200
    _, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    assert window["coins"] == 1 and window["credited_seconds"] == 1
    report.snapshot("coin-window-sixty-seconds-without-coins-sends-close", address, password, opener,
                    real_idle_seconds=time.monotonic() - started)


def coin_credit_after_expiry(opener, report, address, password, client_address, device_ip, vendo):
    assert request(opener, "/admin/validity", "POST", {"value": "15", "unit": "seconds"}).status == 200
    # The tier, not the 15-second site Validity, sets the replacement's deadline.
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=10,2592000,3"}).status == 200
    response = generate_vouchers(opener, {"seconds": "120", "count": "1"})
    code = voucher_codes(opener, response)[0]
    purchase = pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code})
    cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", purchase.get("http-headers", ""), re.I).group(1)
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id}, cookie)
    assert vendo.heartbeat()[0] == 200
    window_id = vendo.window_id
    before = json.load(request(opener, "/admin/state"))
    window = next(w for w in before["coin_windows"] if w["id"] == window_id)
    old_id = window["time_credit_id"]
    old = next(tc for tc in before["purchases"]["time_credits"] if tc["id"] == old_id)
    validity = next(v for v in before["validity"] if v["time_credit_id"] == old_id)
    # Keep the final coin count on the board until after expiry. Reporting it
    # now would let the five-second grace settlement legitimately Top-up early.
    vendo.insert(1)
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200
    pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": window_id}, cookie)
    # Done still has a valid identity; delay the Vendo's final count past its deadline.
    time.sleep(max(0, validity["valid_until"] - time.time()))
    pending = json.load(request(opener, "/admin/state"))
    window = next(w for w in pending["coin_windows"] if w["id"] == window_id)
    assert window["coins"] == 0 and window["credited_seconds"] == 0
    assert next(tc for tc in pending["purchases"]["time_credits"] if tc["id"] == old_id) == old
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    closed = vendo.frame(5)
    assert vendo.post(closed)[0] == 200
    after, settled = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    new_id = settled["time_credit_id"]
    assert new_id != old_id, "coin credit revived its expired target"
    assert next(tc for tc in after["purchases"]["time_credits"] if tc["id"] == old_id) == old
    assert next(v for v in after["validity"] if v["time_credit_id"] == old_id)["valid_until"] == validity["valid_until"]
    assert next(tc for tc in after["purchases"]["time_credits"] if tc["id"] == new_id)["bought_seconds"] == 10
    assert settled["credited_seconds"] == 10
    sale = next(row for row in sales(opener)["rows"]
                if row["type"] == "coin" and row["id"] == str(window_id))
    replacement_validity = next(v for v in after["validity"] if v["time_credit_id"] == new_id)
    assert sale["user"] == new_id, "Sales row did not show the replacement Time Credit"
    assert datetime.datetime.fromisoformat(sale["validity"]).timestamp() == replacement_validity["valid_until"]
    after = wait_for_validity_removal(opener, address, password, old_id, validity["valid_until"])
    assert vendo.post(closed)[0] == 200
    replay = json.load(request(opener, "/admin/state"))
    assert replay["purchases"] == after["purchases"], "Closed replay credited expired-target coins twice"
    portal = pause_device_fetch(client_address, password, device_ip, "/portal/api/state", cookie=cookie)
    state = portal_state(portal)
    assert state["message"] is None, "old browser cookie hid the replacement Time Credit"
    assert state["time_credit"]["bought"] == 10, "portal did not show the replacement Time Credit"
    remaining = state["time_credit"]["remaining"]
    assert 0 < remaining <= 10, "portal did not show the replacement's remaining time"
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", state["time_credit"]["resume_code"]), "replacement Resume Code missing"
    refreshed = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", portal.get("http-headers", ""), re.I)
    assert refreshed and refreshed.group(1) != cookie, "portal did not replace the expired cookie"
    revisit = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state", cookie=refreshed.group(1)))
    assert revisit["time_credit"]["bought"] == 10 and revisit["message"] is None
    report.snapshot("coin-credit-to-expired-target-creates-new-time-credit", address, password, opener,
                    old_time_credit_id=old_id, new_time_credit_id=new_id, old_bought_seconds=120,
                    new_bought_seconds=10, old_deadline_unchanged=True,
                    sales_row=sale, replacement_valid_until=replacement_validity["valid_until"],
                    stale_cookie_shows_replacement=True, replacement_cookie_reissued=True,
                    replacement_remaining_seconds=remaining, replacement_resume_code_shown=True)


def coin_window_departure(opener, report, address, password, client_address, device_ip, vendo):
    opened = pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": vendo.id})
    cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", opened.get("http-headers", ""), re.I).group(1)
    assert vendo.heartbeat()[0] == 200
    window_id = vendo.window_id
    def client(path, method="GET", body=None):
        return router_rest(client_address, "admin", password, path, method, body, scheme="http")
    nic = client("/interface/ethernet?name=lan1")[0]
    hotspot = router_rest(address, "admin", password, "/ip/hotspot?name=hs1")[0]
    profiles = [p for p in router_rest(address, "admin", password, "/ip/hotspot/user/profile")
                if p["name"].startswith("openpiso-")]
    started = time.monotonic()
    try:
        # Only the fixture shortens host/online Device detection. Production
        # Pause profiles and the Router's original host policy are restored below.
        router_rest(address, "admin", password, "/ip/hotspot/" + hotspot[".id"], "PATCH",
                    {"idle-timeout": "5s", "keepalive-timeout": "5s"})
        for profile in profiles:
            router_rest(address, "admin", password, "/ip/hotspot/user/profile/" + profile[".id"], "PATCH",
                        {"keepalive-timeout": "5s"})
        # Profile keepalive changes take effect at the next Router login.
        pause_device_fetch(client_address, password, device_ip, "/portal/api/connect", {}, cookie)
        client("/interface/ethernet/" + nic[".id"], "PATCH", {"disabled": "yes"})
        started = time.monotonic()
        while router_rest(address, "admin", password, "/ip/hotspot/host?address=" + device_ip):
            assert time.monotonic() - started < 45, "Router did not naturally drop the departed Device host"
            time.sleep(0.5)
        departure_seconds = time.monotonic() - started
        _, window = coin_window_wait(opener, window_id, lambda w: w["status"] != "open", timeout=15)
        assert window["close_at"] and time.monotonic() - started < 60, "window closed by coin idle, not departure"
        assert vendo.heartbeat()[0] == 200 and vendo.closing
        assert vendo.heartbeat()[0] == 200
    finally:
        report.capture_lab_failure()
        router_rest(address, "admin", password, "/ip/hotspot/" + hotspot[".id"], "PATCH",
                    {key: hotspot[key] for key in ("idle-timeout", "keepalive-timeout")})
        for profile in profiles:
            router_rest(address, "admin", password, "/ip/hotspot/user/profile/" + profile[".id"], "PATCH",
                        {"keepalive-timeout": profile["keepalive-timeout"]})
        client("/interface/ethernet/" + nic[".id"], "PATCH", {"disabled": "no"})
        device_ip = rebind_wait_device(client_address, password)
        rebind_fetch(client_address, password, device_ip, "/portal/api/connect", {}, cookie)
    report.snapshot("coin-window-real-device-departure-sends-close", address, password, opener,
                    router_host_expired_naturally=True, departure_seconds=departure_seconds,
                    host_timeout_seconds=5, manual_host_delete=False, final_coins=0)
    return device_ip


def vendo_row(page: str, vendo_id) -> str:
    """One Vendo's detail source, including its nested Edit template."""
    return re.search(rf'<template data-vendo-source="{vendo_id}">(.*?)</article></template>', page, re.S).group(1) + "</article>"


def vendo_list_details(opener, report, address, password, client_address):
    """Vendo List (#89): rename, Status with Last seen, and the Edit dialog's Details and Unpair."""
    page = request(opener, "/admin/vendos").read().decode()
    assert "<title>Vendo List" in page and '<h3>Vendo List</h3>' in page
    assert re.search(r'href="/admin/vendos"[^>]*>.*?<span class="menu-text">Vendo List</span>', page, re.S)
    for retired in ("/admin/vendos/name", "/admin/vendos/server"):
        assert request(opener, retired, "POST", {"id": "1", "name": "x"}).status in (404, 405)

    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    assert vendo.heartbeat()[0] == 200
    stored = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)
    page = request(opener, "/admin/vendos").read().decode()
    row = vendo_row(page, vendo.id)
    assert "Online" in row and "Last seen" in row, row
    assert f'data-select="{vendo.id}"' in page and 'data-detail-slot' in page, page
    assert '<dt>Profile</dt><dd>Base profiles</dd>' in row, row
    edit = re.search(r'<form\b[^>]*action="/admin/vendos/edit".*?</form>', row, re.S).group(0)
    # The first field takes focus; IP and MAC are shown but never submitted.
    assert re.search(r'<input\b[^>]*name="name"[^>]*autofocus', edit), edit
    assert 'name="location"' in edit and 'maxlength="80"' in edit and 'name="hotspot_server"' in edit
    assert stored["ip"] in edit and stored["mac"] in edit
    assert not re.search(r'name="(ip|mac)"', edit), edit
    assert f'data-hold="{vendo.id}"' in row and 'Press and hold for 3 seconds' in row, row
    servers = re.findall(r'<option value="([^"]+)"', re.search(r'<select name="hotspot_server".*?</select>', edit, re.S).group(0))
    assert stored["hotspot_server"] in servers, (stored, servers)

    form = {"id": vendo.id, "name": "  Plaza  ", "location": "  Near the sari-sari store  ",
            "hotspot_server": stored["hotspot_server"], "ip": "10.9.9.9", "mac": "02:00:00:00:00:99"}
    response = request(opener, "/admin/vendos/edit", "POST", form)
    assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/vendos", response.status
    edited = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)
    assert (edited["name"], edited["location"]) == ("Plaza", "Near the sari-sari store"), edited
    assert (edited["ip"], edited["mac"], edited["hotspot_server"]) == (stored["ip"], stored["mac"], stored["hotspot_server"])
    row = vendo_row(request(opener, "/admin/vendos").read().decode(), vendo.id)
    assert "Plaza" in row and "Near the sari-sari store" in row
    selector = request(opener, "/admin/sales").read().decode()
    options = dict(re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', re.search(r'<select name="vendo"[^>]*>(.*?)</select>', selector, re.S).group(1)))
    assert options[str(vendo.id)] == "Plaza", options
    for rejected, status in (({**form, "name": "X" * 81}, 400), ({**form, "location": "X" * 81}, 400),
                             ({**form, "hotspot_server": "missing-server"}, 400), ({**form, "id": "0"}, 400),
                             ({**form, "id": "4294967295"}, 404)):
        assert request(opener, "/admin/vendos/edit", "POST", rejected).status == status, rejected
    assert request(urllib.request.build_opener(), "/admin/vendos/edit", "POST", form).status == 401
    # A form without the server (its select is disabled while the Router is down) keeps it.
    assert request(opener, "/admin/vendos/edit", "POST", {"id": vendo.id, "name": "Plaza", "location": "Gate"}).status == 200
    kept = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)
    assert (kept["location"], kept["hotspot_server"]) == ("Gate", stored["hotspot_server"]), kept
    report.snapshot("vendo-list-edit-details", address, password, opener, vendo_id=vendo.id, servers=servers)

    time.sleep(11)  # past the 10 s Online threshold with no report
    row = vendo_row(request(opener, "/admin/vendos").read().decode(), vendo.id)
    assert 'data-status="Offline"' in row and "Last seen" in row, row
    report.snapshot("vendo-list-stale-vendo-offline", address, password, opener, row=row)

    # The Operator's browser: focus, Esc and the close button, then Unpair from the footer.
    out = report.path.parent / "vendo-list"
    out.mkdir(parents=True, exist_ok=True)
    _browser_module.ensure_image()
    browser = _browser_module.run_browser("admin", ["--network", "host"], BASE_URL, "vendos",
                                          {"password": ADMIN_PASSWORD, "vendo_id": vendo.id}, out, {}, report)
    unpaired = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)
    assert unpaired["status"] == "unpaired", unpaired
    row = vendo_row(request(opener, "/admin/vendos").read().decode(), vendo.id)
    assert 'data-status="Unpaired"' in row and "data-edit-source" not in row and "data-hold=" not in row, row
    assert request(opener, "/admin/vendos/edit", "POST", {"id": vendo.id, "name": "x", "location": ""}).status == 404
    report.data["vendo_list"] = {"screenshots": [f"vendo-list/{name}.png" for name in browser["screenshots"]],
                                 "browser": browser}
    report.snapshot("vendo-list-unpair-from-dialog", address, password, opener, browser=browser)


def vendo_list_deactivate(opener, report, address, password, client_address):
    """Deactivate and Reactivate (#90): Insert Coin refuses with the out-of-service
    message, rule 1 still counts paired Vendos, and Sales never change."""
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)

        def windows():
            return json.load(request(opener, "/admin/state"))["coin_windows"]

        def coin_sales():
            """Coin Sales by id, as fields a later Top-up must never rewrite."""
            return {row["id"]: (row["at"], row["vendo_id"], row["amount"], row["credited_seconds"])
                    for row in sales(opener)["rows"] if row["type"] == "coin"}

        def assert_sales_kept(history):
            current = coin_sales()
            assert {key: current.get(key) for key in history} == history, "Sales history changed"

        def buy(vlan, vendo):
            """Insert Coin on the VLAN's Device, one coin, Done: one credited Sale on vendo."""
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {}))
            assert opened["coin_window"]["vendo_id"] == vendo.id, opened
            window_id = opened["coin_window"]["id"]
            assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
            vendo.insert(1)
            assert vendo.heartbeat()[0] == 200
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/done", {"window_id": window_id})
            assert vendo.heartbeat()[0] == 200
            assert vendo.heartbeat()[0] == 200
            coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
            assert str(window_id) in coin_sales()

        def refused(vlan, status):
            """Insert Coin is refused with this HTTP status and reserves nothing.
            The Device's /tool/fetch reports only the status: 503 is the
            out-of-service refusal, 400 is "No Vendo serves this network"."""
            before = windows()
            try:
                pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {})
            except urllib.error.HTTPError as error:
                detail = json.load(error).get("detail", "")
                assert str(status) in detail, detail
            else:
                raise AssertionError(f"Insert Coin on VLAN {vlan} was accepted")
            assert windows() == before, "a refused Insert Coin reserved a Vendo"
            assert portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))["vendos"] == []

        def set_active(vendo, active, htmx):
            form = {"id": vendo.id, "active": "1" if active else "0"}
            if not htmx:
                response = request(opener, "/admin/vendos/active", "POST", form)
                assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/vendos", response.status
                return vendo_row(response.read().decode(), vendo.id)
            response = request(opener, "/admin/vendos/active", "POST", form, {"HX-Request": "true"})
            body = response.read().decode()
            assert response.status == 200 and body.lstrip().startswith(f'<div id="vendo-{vendo.id}"'), (response.status, body)
            return body

        def stored(vendo):
            return next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)

        board = pair_board(opener, client_address, password, fixture.board(10, 1))
        paired.append(board)
        first = SimulatedVendo(BASE_URL, board["id"], bytes.fromhex(board["secret"]), timeout=HTTP_TIMEOUT)
        assert first.heartbeat()[0] == 200
        buy(10, first)
        history = coin_sales()

        # The site's only paired Vendo: deactivating it refuses Insert Coin everywhere,
        # as out of service rather than as an unpaired site.
        row = set_active(first, False, htmx=True)
        assert "Deactivated" in row and 'action="/admin/vendos/active"' in row and 'name="active" value="1"' in row, row
        assert 'data-vendo-action="power"' in row and re.search(r'\bReactivate</button>', row), row
        assert stored(first)["status"] == "paired" and stored(first)["active"] is False
        for vlan in (10, 20):
            refused(vlan, 503)
        # Its reports are still accepted, and the Status stays Deactivated while it is Online.
        assert first.heartbeat()[0] == 200
        row = vendo_row(request(opener, "/admin/vendos").read().decode(), first.id)
        assert 'data-status="Deactivated"' in row and "Last seen" in row, row
        assert_sales_kept(history)
        report.snapshot("vendo-deactivate-lone-vendo-out-of-service", address, password, opener, row=row)

        assert first.heartbeat()[0] == 200
        row = set_active(first, True, htmx=False)
        assert "Online" in row and 'name="active" value="0"' in row, row
        buy(10, first)
        assert_sales_kept(history)
        history = coin_sales()
        report.snapshot("vendo-reactivate-opens-coin-window", address, password, opener)

        # Two Vendos: deactivating one leaves its network out of service, and the
        # other keeps serving only its own HotSpot server.
        second_board = pair_board(opener, client_address, password, fixture.board(20, 2))
        paired.append(second_board)
        second = SimulatedVendo(BASE_URL, second_board["id"], bytes.fromhex(second_board["secret"]), timeout=HTTP_TIMEOUT)
        assert second.heartbeat()[0] == 200
        set_active(first, False, htmx=False)
        refused(10, 503)
        state = portal_state(pause_device_fetch(client_address, password, ips[20], "/portal/api/state"))
        assert vendo_ids(state) == [second.id], state
        buy(20, second)
        assert_sales_kept(history)
        history = coin_sales()
        set_active(first, True, htmx=True)
        buy(10, first)
        assert_sales_kept(history)
        report.snapshot("vendo-deactivate-one-of-two-keeps-servers-apart", address, password, opener)

        # Every htmx answer is row HTML, errors included.
        for form, status in (({"id": "4294967295", "active": "0"}, 404), ({"id": first.id, "active": "x"}, 400)):
            response = request(opener, "/admin/vendos/active", "POST", form, {"HX-Request": "true"})
            body = response.read().decode()
            assert response.status == status and body.lstrip().startswith(("<div", "<p")) and 'role="alert"' in body, (status, body)
        assert request(urllib.request.build_opener(), "/admin/vendos/active", "POST", {"id": first.id, "active": "0"}).status == 401
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()


def vendo_list_rates(opener, report, address, password, client_address):
    """Per-Vendo Rates tab and Portal rates (#95): each Device's Portal shows,
    and is charged, the rates of the Vendo serving it; the picker carries each
    Vendo's rates; "Use Shop tiers" brings site pricing back on the next Coin
    Window; the Edit dialog's tabs work from the keyboard."""
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    site = [{"pesos": 1, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1}]
    own = [{"pesos": 1, "seconds": 120, "validity_seconds": 7200, "pause_limit": 2},
           {"pesos": 5, "seconds": 900, "validity_seconds": 86400, "pause_limit": 5}]
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        assert request(opener, "/admin/rates", "POST", {"tiers": "1=60,3600,1"}).status == 200

        def stored(vendo):
            return next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)

        def rates(vendo, form, status=200):
            response = request(opener, "/admin/vendos/rates", "POST", {"id": vendo.id, **form})
            assert response.status == status, (form, response.status)
            if status == 200:
                assert urllib.parse.urlsplit(response.url).path == "/admin/vendos", response.url

        def portal(vlan):
            return portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))

        def buy(vlan, vendo, pesos, before_done=None):
            """A fresh Device on the VLAN opens a Coin Window on vendo and pays: its credited seconds."""
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {"vendo_id": vendo.id}))
            window_id = opened["coin_window"]["id"]
            assert opened["coin_window"]["vendo_id"] == vendo.id, opened
            assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
            if before_done:
                before_done()
            vendo.insert(pesos)
            assert vendo.heartbeat()[0] == 200
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/done", {"window_id": window_id})
            assert vendo.heartbeat()[0] == 200
            assert vendo.heartbeat()[0] == 200
            return coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")[1]["credited_seconds"]

        boards = [pair_board(opener, client_address, password, fixture.board(vlan, n)) for n, vlan in ((1, 10), (2, 20))]
        paired.extend(boards)
        first, second = (SimulatedVendo(BASE_URL, b["id"], bytes.fromhex(b["secret"]), timeout=HTTP_TIMEOUT) for b in boards)
        for vendo in (first, second):
            assert vendo.heartbeat()[0] == 200

        # Until customized, a Vendo uses the site rates; the Rates tab says so.
        assert stored(first)["rate_tiers"] is None
        row = vendo_row(request(opener, "/admin/vendos").read().decode(), first.id)
        assert 'role="tablist"' in row and len(re.findall(r'role="tab"', row)) == 3, row
        assert re.search(r'role="tab"[^>]*aria-selected="true"[^>]*>Details<', row), row
        assert len(re.findall(r'role="tabpanel"', row)) == 3 and 'data-edit-tab="rates"' in row, row
        assert "Using Shop tiers" in row and re.search(r'<form\b[^>]*action="/admin/vendos/rates"', row), row

        # Customize copies the site tiers in; the same validation as /admin/rates applies.
        rates(first, {"action": "customize"})
        assert stored(first)["rate_tiers"] == site, stored(first)
        row = vendo_row(request(opener, "/admin/vendos").read().decode(), first.id)
        assert 'data-tiers="rates"' in row and 'data-cost="1" data-seconds="60" data-validity="3600" data-pause="1"' in row, row
        assert "Use Shop tiers" in row and "Using Shop Points table" in row and "<textarea" not in row, row
        for invalid in ("2=60", "1=60,3600", "1=60,0,1", "1=60,3600,1001", "1=60\n5=200",
                        "1=60,3600,1,0.2\n5=300,3600,1,0.5"):  # fewer Points per peso at the higher tier
            rates(first, {"action": "save", "tiers": invalid}, 400)
        rates(first, {"action": "save", "tiers": "1=120,7200,2\n5=900,86400,5"})
        assert stored(first)["rate_tiers"] == own, stored(first)
        assert stored(second)["rate_tiers"] is None
        for form, status in (({"id": "4294967295", "action": "customize"}, 404), ({"id": first.id, "action": "x"}, 400)):
            assert request(opener, "/admin/vendos/rates", "POST", form).status == status, form
        assert request(urllib.request.build_opener(), "/admin/vendos/rates", "POST", {"id": first.id, "action": "site"}).status == 401
        # Saving Rates never resubmits Details.
        assert (stored(first)["name"], stored(first)["hotspot_server"]) == ("", "opnw-vlan10"), stored(first)

        # Two Vendos on two servers: each Device sees, and is charged, its own Vendo's rates.
        assert portal(10)["rates"] == own and portal(10)["vendos"] == [{"id": first.id, "rates": own}], portal(10)
        assert portal(20)["rates"] == site and portal(20)["vendos"] == [{"id": second.id, "rates": site}], portal(20)
        assert buy(10, first, 5) == 900 and buy(20, second, 5) == 300
        report.snapshot("vendo-rates-two-servers-each-charged-own-rates", address, password, opener)

        # Two Vendos on one server: the picker carries each Vendo's own rates.
        assert request(opener, "/admin/vendos/edit", "POST", {
            "id": second.id, "name": "", "location": "", "hotspot_server": "opnw-vlan10"}).status == 200
        picker = portal(10)
        assert picker["vendos"] == [{"id": first.id, "rates": own}, {"id": second.id, "rates": site}], picker
        assert picker["rates"] == own, picker  # the first Vendo of several
        assert portal(20)["rates"] == site and portal(20)["vendos"] == [], portal(20)
        assert buy(10, second, 1) == 60 and buy(10, first, 1) == 120
        report.snapshot("vendo-rates-one-server-picker-shows-each-vendos-rates", address, password, opener, picker=picker)

        # "Use Shop tiers" during a live window keeps that window's snapshot; the next one is site-priced.
        assert buy(10, first, 1, lambda: rates(first, {"action": "site"})) == 120
        assert stored(first)["rate_tiers"] is None
        assert buy(10, first, 1) == 60
        assert picker != portal(10) and portal(10)["vendos"][0] == {"id": first.id, "rates": site}
        report.snapshot("vendo-rates-use-site-rates-next-coin-window", address, password, opener)

        # The Operator's browser: the tabs from the keyboard, then Customize from the Rates tab.
        out = report.path.parent / "vendo-rates"
        out.mkdir(parents=True, exist_ok=True)
        _browser_module.ensure_image()
        browser = _browser_module.run_browser("admin", ["--network", "host"], BASE_URL, "vendo-rates",
                                              {"password": ADMIN_PASSWORD, "vendo_id": first.id}, out, {}, report)
        assert stored(first)["rate_tiers"] == site, stored(first)
        report.data["vendo_rates"] = {"screenshots": [f"vendo-rates/{name}.png" for name in browser["screenshots"]],
                                      "browser": browser}
        report.snapshot("vendo-rates-tabs-from-keyboard", address, password, opener, browser=browser)
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()


def vendo_list_points_tab(opener, report, address, password, client_address):
    """Per-Vendo Points tab (#98): a Vendo uses the site Points table until the
    Operator customizes it; a Device redeems from the table of the Vendo serving
    its HotSpot server, or the site's when none does (a Deactivated Vendo serves
    none); "Use Shop Points table" restores the site table. Leaves the site rates and
    Points table as found."""
    found = json.load(request(opener, "/admin/state"))
    milli = lambda m: f"{m // 1000}.{m % 1000:03d}".rstrip("0").rstrip(".")
    saved_rates = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]},{milli(t.get("points_milli", 0))}'
                            for t in found["rate_tiers"])
    saved_points = "\n".join(f'{milli(t["points_milli"])}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]}'
                             for t in found["points_tiers"])
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    site = [{"points_milli": 1000, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1}]
    own = [{"points_milli": 2000, "seconds": 300, "validity_seconds": 7200, "pause_limit": 3}]
    other = [{"points_milli": 1500, "seconds": 120, "validity_seconds": 3600, "pause_limit": 2}]
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        # 1 Point per ₱1, and a site Points table of 1 Point for a minute.
        assert request(opener, "/admin/rates", "POST", {"tiers": "1=60,3600,1,1"}).status == 200
        assert request(opener, "/admin/points", "POST", {"tiers": "1=60,3600,1"}).status == 200

        def stored(vendo):
            return next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)

        def points(vendo, form, status=200):
            response = request(opener, "/admin/vendos/points", "POST", {"id": vendo.id, **form})
            assert response.status == status, (form, response.status)
            if status == 200:
                assert urllib.parse.urlsplit(response.url).path == "/admin/vendos", response.url

        def row(vendo):
            return vendo_row(request(opener, "/admin/vendos").read().decode(), vendo.id)

        def portal(vlan):
            return portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))

        def earn(vlan, vendo, pesos):
            """The VLAN's Device pays pesos at vendo: its Time Credit's balance afterwards."""
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {"vendo_id": vendo.id}))
            window_id = opened["coin_window"]["id"]
            assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
            vendo.insert(pesos)
            assert vendo.heartbeat()[0] == 200
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/done", {"window_id": window_id})
            assert vendo.heartbeat()[0] == 200
            assert vendo.heartbeat()[0] == 200
            coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
            return portal(vlan)["time_credit"]["points_milli"]

        def redeem(vlan, tier):
            form = {"points_milli": tier["points_milli"], "seconds": tier["seconds"]}
            before = portal(vlan)["time_credit"]
            after = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/points/redeem", form))["time_credit"]
            assert after["points_milli"] == before["points_milli"] - tier["points_milli"], (before, after)
            assert after["bought"] == before["bought"] + tier["seconds"] and after["pause_limit"] == tier["pause_limit"], (before, after)

        def refused(vlan, tier):
            """A row outside the Device's Points table is refused and changes nothing."""
            form = {"points_milli": tier["points_milli"], "seconds": tier["seconds"]}
            before = portal(vlan)["time_credit"]
            try:
                pause_device_fetch(client_address, password, ips[vlan], "/portal/api/points/redeem", form)
            except urllib.error.HTTPError as error:
                detail = json.load(error).get("detail", "")
                assert "400" in detail, detail  # /tool/fetch reports only the status: points-row-unknown
                after = portal(vlan)["time_credit"]
                kept = ("bought", "points_milli", "pause_limit", "pauses_left", "valid_until")
                assert {k: after[k] for k in kept} == {k: before[k] for k in kept}, (before, after)
                return
            raise AssertionError(f"VLAN {vlan} redeemed a row outside its Points table")

        boards = [pair_board(opener, client_address, password, fixture.board(vlan, n)) for n, vlan in ((1, 10), (2, 20))]
        paired.extend(boards)
        first, second = (SimulatedVendo(BASE_URL, b["id"], bytes.fromhex(b["secret"]), timeout=HTTP_TIMEOUT) for b in boards)
        for vendo in (first, second):
            assert vendo.heartbeat()[0] == 200

        # Until customized, a Vendo uses the site Points table; the Points tab says so.
        assert stored(first)["points_tiers"] is None
        html = row(first)
        assert len(re.findall(r'role="tab"', html)) == 3 and re.search(r'role="tab"[^>]*>Points table<', html), html
        assert len(re.findall(r'role="tabpanel"', html)) == 3, html
        assert re.search(r'<form\b[^>]*action="/admin/vendos/points"', html) and "Using Shop Points table" in html and "Using Shop tiers" in html, html

        # Customize copies the site table in; the /admin/points rules apply; Rates and Details stay.
        points(first, {"action": "customize"})
        assert stored(first)["points_tiers"] == site, stored(first)
        html = row(first)
        assert "Using Shop tiers" in html and 'data-tiers="points"' in html and "Use Shop Points table" in html, html
        assert 'data-cost="1" data-seconds="60" data-validity="3600" data-pause="1"' in html and "<textarea" not in html, html
        for invalid in ("1=60", "0=60,3600,1", "0.0001=60,3600,1", "1=0,3600,1", "1=60,3600,1001", "1=60,soon,1"):
            points(first, {"action": "save", "tiers": invalid}, 400)
        points(first, {"action": "save", "tiers": "2=300,7200,3"})
        assert stored(first)["points_tiers"] == own and stored(second)["points_tiers"] is None, stored(first)
        assert stored(first)["rate_tiers"] is None and stored(first)["hotspot_server"] == "opnw-vlan10", stored(first)
        for form, status in (({"id": "4294967295", "action": "customize"}, 404), ({"id": first.id, "action": "x"}, 400)):
            assert request(opener, "/admin/vendos/points", "POST", form).status == status, form
        assert request(urllib.request.build_opener(), "/admin/vendos/points", "POST", {"id": first.id, "action": "site"}).status == 401
        report.snapshot("vendo-points-customize-and-save", address, password, opener, points_tiers=own)

        # A Device on the customized Vendo's server sees and redeems from its table, not the site's.
        assert portal(10)["points"] == own and portal(20)["points"] == site, (portal(10), portal(20))
        earn(10, first, 5)
        refused(10, site[0])
        redeem(10, own[0])
        report.snapshot("vendo-points-device-redeems-from-its-vendos-table", address, password, opener)

        # A server whose only Vendo is Deactivated has no serving Vendo: the site table applies.
        points(second, {"action": "customize"})
        points(second, {"action": "save", "tiers": "1.5=120,3600,2"})
        assert portal(20)["points"] == other
        earn(20, second, 5)
        assert request(opener, "/admin/vendos/active", "POST", {"id": second.id, "active": "0"}).status == 200
        assert portal(20)["points"] == site and portal(20)["vendos"] == [], portal(20)
        refused(20, other[0])
        redeem(20, site[0])
        assert request(opener, "/admin/vendos/active", "POST", {"id": second.id, "active": "1"}).status == 200
        report.snapshot("vendo-points-no-serving-vendo-uses-site-table", address, password, opener)

        # Use site rates drops the Vendo's own table: the site table is back, on the tab and the Portal.
        points(first, {"action": "site"})
        assert stored(first)["points_tiers"] is None
        assert "Using Shop Points table" in row(first).split('data-edit-panel="points"')[1], row(first)
        assert portal(10)["points"] == site
        refused(10, own[0])
        redeem(10, site[0])
        report.snapshot("vendo-points-use-site-rates-restores-site-table", address, password, opener)
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()
        assert request(opener, "/admin/rates", "POST", {"tiers": saved_rates}).status == 200
        assert request(opener, "/admin/points", "POST", {"tiers": saved_points}).status == 200


def shop_defaults_vendo_list(opener, report, address, password, client_address):
    """Vendo List in Shop wording (#107): a Vendo that follows the Shop reads
    "Shop tiers" / "Shop table" with no badge; Customize for this Vendo starts
    from a copy of the Shop set and adds the "Custom tiers" / "Custom Points"
    badge; in the Operator's browser, Use Shop tiers and Use Shop Points table
    confirm with what is discarded, clear the Vendo's own set, and its next Coin
    Window and redemption use the Shop's. Leaves the Shop tiers and table as found."""
    found = json.load(request(opener, "/admin/state"))
    milli = lambda m: f"{m // 1000}.{m % 1000:03d}".rstrip("0").rstrip(".")
    saved_rates = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]},{milli(t.get("points_milli", 0))}'
                            for t in found["rate_tiers"])
    saved_points = "\n".join(f'{milli(t["points_milli"])}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]}'
                             for t in found["points_tiers"])
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    shop_rates = [{"pesos": 1, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1, "points_milli": 1000}]
    shop_points = [{"points_milli": 1000, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1}]
    own_points = [{"points_milli": 2000, "seconds": 300, "validity_seconds": 7200, "pause_limit": 3}]
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        # Shop: ₱1 for a minute earning 1 Point, and 1 Point redeems a minute.
        assert request(opener, "/admin/rates", "POST", {"tiers": "1=60,3600,1,1"}).status == 200
        assert request(opener, "/admin/points", "POST", {"tiers": "1=60,3600,1"}).status == 200

        def stored(vendo):
            return next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)

        def post(path, vendo, form):
            response = request(opener, path, "POST", {"id": vendo.id, **form})
            assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/vendos", (path, form, response.status)

        def page():
            return request(opener, "/admin/vendos").read().decode()

        def item(html, vendo):
            """The Vendo's row in the list."""
            return re.search(rf'<button type="button" class="item[^"]*" data-select="{vendo.id}".*?</button>', html, re.S).group(0)

        def header(html, vendo):
            """The Vendo's detail header, above its status line."""
            return vendo_row(html, vendo.id).split('class="vcard-head"', 1)[1].split("data-vendo-close", 1)[0]

        def text(html):
            return re.sub(r"<[^>]+>", " ", html)

        def portal(vlan):
            return portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))

        def buy(vlan, vendo, pesos):
            """The VLAN's Device opens a Coin Window on vendo and pays: its credited seconds."""
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {"vendo_id": vendo.id}))
            window_id = opened["coin_window"]["id"]
            assert opened["coin_window"]["vendo_id"] == vendo.id, opened
            assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
            vendo.insert(pesos)
            assert vendo.heartbeat()[0] == 200
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/done", {"window_id": window_id})
            assert vendo.heartbeat()[0] == 200
            assert vendo.heartbeat()[0] == 200
            return coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")[1]["credited_seconds"]

        def redeem(vlan, tier):
            form = {"points_milli": tier["points_milli"], "seconds": tier["seconds"]}
            before = portal(vlan)["time_credit"]
            after = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/points/redeem", form))["time_credit"]
            assert after["points_milli"] == before["points_milli"] - tier["points_milli"], (before, after)
            assert after["bought"] == before["bought"] + tier["seconds"], (before, after)

        def refused(vlan, tier):
            """A row outside the Device's Points table is refused and changes nothing."""
            form = {"points_milli": tier["points_milli"], "seconds": tier["seconds"]}
            before = portal(vlan)["time_credit"]
            try:
                pause_device_fetch(client_address, password, ips[vlan], "/portal/api/points/redeem", form)
            except urllib.error.HTTPError as error:
                assert "400" in json.load(error).get("detail", "")
                assert portal(vlan)["time_credit"]["points_milli"] == before["points_milli"]
                return
            raise AssertionError(f"VLAN {vlan} redeemed a row outside its Points table")

        boards = [pair_board(opener, client_address, password, fixture.board(vlan, n)) for n, vlan in ((1, 10), (2, 20))]
        paired.extend(boards)
        custom, follower = (SimulatedVendo(BASE_URL, b["id"], bytes.fromhex(b["secret"]), timeout=HTTP_TIMEOUT) for b in boards)
        for vendo in (custom, follower):
            assert vendo.heartbeat()[0] == 200

        # A Vendo that follows the Shop: Shop wording, glossary tabs, no badge, no "site".
        html = page()
        for vendo in (custom, follower):
            row = vendo_row(html, vendo.id)
            for badge in ("Custom tiers", "Custom Points"):
                assert f">{badge}<" not in item(html, vendo) and f">{badge}<" not in row, (badge, row)
            assert "<dd>Shop tiers</dd>" in row and "<dd>Shop table</dd>" in row, row
            assert re.search(r'role="tab"[^>]*>Rate tiers<', row) and re.search(r'role="tab"[^>]*>Points table<', row), row
            assert len(re.findall(r">Customize for this Vendo<", row)) == 2, row
            assert "Starts from a copy of the Shop tiers." in row and "Starts from a copy of the Shop Points table." in row, row
        assert not re.search(r"\bsites?\b", text(html), re.I), re.findall(r".{40}\bsites?\b.{40}", text(html), re.I)
        report.snapshot("shop-vendo-list-follower-shop-wording", address, password, opener)

        # Customize for this Vendo copies the Shop tiers; the row and header gain "Custom tiers".
        post("/admin/vendos/rates", custom, {"action": "customize"})
        assert stored(custom)["rate_tiers"] == shop_rates and stored(follower)["rate_tiers"] is None, stored(custom)
        html = page()
        row = vendo_row(html, custom.id)
        assert 'data-tiers="rates"' in row and 'data-cost="1" data-seconds="60" data-validity="3600" data-pause="1" data-points="1"' in row, row
        assert ">Custom tiers<" in item(html, custom) and ">Custom tiers<" in header(html, custom), row
        assert ">Custom Points<" not in item(html, custom), item(html, custom)
        assert "<dd>Custom · 1 tier</dd>" in row and "<dd>Shop table</dd>" in row and ">Use Shop tiers<" in row, row
        assert ">Custom tiers<" not in item(html, follower), item(html, follower)
        post("/admin/vendos/rates", custom, {"action": "save", "tiers": "1=120,7200,2,1"})
        assert buy(10, custom, 5) == 600 and buy(20, follower, 1) == 60

        # The same for the Points table: a copy, then the Vendo's own row.
        post("/admin/vendos/points", custom, {"action": "customize"})
        assert stored(custom)["points_tiers"] == shop_points and stored(follower)["points_tiers"] is None, stored(custom)
        html = page()
        row = vendo_row(html, custom.id)
        assert 'data-tiers="points"' in row and 'data-cost="1" data-seconds="60" data-validity="3600" data-pause="1"' in row, row
        assert ">Custom Points<" in item(html, custom) and ">Custom Points<" in header(html, custom), row
        assert "<dd>Custom · 1 row</dd>" in row and ">Use Shop Points table<" in row, row
        post("/admin/vendos/points", custom, {"action": "save", "tiers": "2=300,7200,3"})
        assert portal(10)["points"] == own_points and portal(20)["points"] == shop_points, (portal(10), portal(20))
        refused(10, shop_points[0])
        redeem(10, own_points[0])
        report.snapshot("shop-vendo-list-customize-copies-shop-set", address, password, opener)

        # The Operator's browser: Use Shop tiers and Use Shop Points table, through their confirms.
        out = report.path.parent / "shop-vendo-list"
        out.mkdir(parents=True, exist_ok=True)
        _browser_module.ensure_image()
        browser = _browser_module.run_browser("admin", ["--network", "host"], BASE_URL, "shop-vendo-list",
                                              {"password": ADMIN_PASSWORD, "vendo_id": custom.id}, out, {}, report)
        assert stored(custom)["rate_tiers"] is None and stored(custom)["points_tiers"] is None, stored(custom)

        # Cleared: Shop wording and no badge again; the next Coin Window and redemption use the Shop's.
        html = page()
        row = vendo_row(html, custom.id)
        assert ">Custom tiers<" not in item(html, custom) and ">Custom Points<" not in item(html, custom), item(html, custom)
        assert "<dd>Shop tiers</dd>" in row and "<dd>Shop table</dd>" in row, row
        assert buy(10, custom, 5) == 300
        assert portal(10)["points"] == shop_points, portal(10)
        refused(10, own_points[0])
        redeem(10, shop_points[0])
        report.data["shop_vendo_list"] = {"screenshots": [f"shop-vendo-list/{name}.png" for name in browser["screenshots"]],
                                          "browser": browser}
        report.snapshot("shop-vendo-list-use-shop-clears-and-reprices", address, password, opener, browser=browser)
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()
        assert request(opener, "/admin/rates", "POST", {"tiers": saved_rates}).status == 200
        assert request(opener, "/admin/points", "POST", {"tiers": saved_points}).status == 200


def vendo_list_coins_out(opener, report, address, password, client_address, device_ip):
    """Sales columns and Coins Out (#91): coins in and out of a Coin Window raise
    Current and Total Sales; Coins Out records the confirmed amount, zeroes
    Current Sales and leaves Total Sales; an unpaired row keeps Coins Out."""
    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
    assert vendo.heartbeat()[0] == 200

    def row():
        return vendo_row(request(opener, "/admin/vendos").read().decode(), vendo.id)

    def columns(html):
        """Last Coins Out, Current Sales and Total Sales in the detail pane."""
        last = re.search(r'<span\b[^>]*data-last-coins-out[^>]*>(.*?)</span>', html, re.S).group(1)
        current = re.search(r'Current Sales</span><strong[^>]*>(.*?)</strong>', html, re.S).group(1)
        total = re.search(r'<dt>Total Sales</dt><dd>(.*?)</dd>', html, re.S).group(1)
        return tuple(" ".join(re.sub(r"<[^>]+>", " ", cell).split()) for cell in (last, current, total))

    def coins_out_form(html):
        return re.search(r'<form\b[^>]*action="/admin/vendos/coins-out".*?</form>', html, re.S).group(0)

    def coins_out(form_html, status=200):
        form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form_html))
        response = request(opener, "/admin/vendos/coins-out", "POST", form, {"HX-Request": "true"})
        body = response.read().decode()
        assert response.status == status and body.lstrip().startswith(f'<div id="vendo-{vendo.id}"'), (response.status, body)
        return body

    def stray(pulses):
        """Coins dropped with no Coin Window open: audit only, but in the box."""
        vendo.total += pulses
        assert vendo.heartbeat()[0] == 200

    assert columns(row()) == ("No Coins Out yet", "₱0", "₱0"), columns(row())
    # ₱5 through a Coin Window and ₱2 outside one: Current Sales matches the box.
    fetch = lambda path, form=None: pause_device_fetch(client_address, password, device_ip, path, form)
    fetch("/portal/api/coins/open", {"vendo_id": paired["id"]})
    assert vendo.heartbeat()[0] == 200 and vendo.window_id
    window_id = vendo.window_id
    vendo.insert(5)
    assert vendo.heartbeat()[0] == 200
    fetch("/portal/api/coins/done", {"window_id": window_id})
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    assert vendo.heartbeat()[0] == 200
    coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
    stray(2)
    before = row()
    assert columns(before) == ("No Coins Out yet", "₱7", "₱7"), columns(before)

    # The confirmation names the amount, and Coins Out records exactly that,
    # even when a coin lands between showing the row and confirming.
    form = coins_out_form(before)
    assert 'data-vendo-action="coins"' in form and 'data-amount="7"' in form, form
    stray(1)
    swapped = coins_out(form)
    last, current, total = columns(swapped)
    assert re.fullmatch(r"Last Coins Out ₱7 · \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", last) and (current, total) == ("₱1", "₱8"), columns(swapped)
    stale = coins_out_form(swapped)
    swapped = coins_out(stale)
    assert columns(swapped)[1:] == ("₱0", "₱8") and columns(swapped)[0].startswith("Last Coins Out ₱1 · "), columns(swapped)
    assert "/admin/vendos/coins-out" not in swapped, "Coins Out offered with nothing to collect"
    report.snapshot("vendo-coins-out-records-confirmed-amount", address, password, opener, row=columns(swapped))

    # Further coins count from ₱0; posting a used confirmation again has nothing to record.
    stray(3)
    assert columns(row())[1:] == ("₱3", "₱11"), columns(row())
    refused = coins_out(stale, 409)
    assert 'role="alert"' in refused and columns(refused)[1:] == ("₱3", "₱11"), refused
    for form, status in (({"id": "4294967295", "through": "1"}, 404), ({"id": vendo.id, "through": "x"}, 400),
                         ({"id": vendo.id, "through": "9223372036854775807"}, 400)):
        response = request(opener, "/admin/vendos/coins-out", "POST", form, {"HX-Request": "true"})
        body = response.read().decode()
        assert response.status == status and body.lstrip().startswith(("<div", "<p")) and 'role="alert"' in body, (status, body)
    assert request(urllib.request.build_opener(), "/admin/vendos/coins-out", "POST", {"id": vendo.id, "through": "1"}).status == 401

    # Unpaired: Coins Out is the row's only action, and a plain post redirects back.
    assert request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]}).status == 204
    unpaired = row()
    assert "Unpaired" in unpaired and "<dialog" not in unpaired and "/admin/vendos/active" not in unpaired, unpaired
    assert unpaired.count('action="/admin/vendos/coins-out"') == 1 and "data-vendo-edit" not in unpaired and "data-hold=" not in unpaired, unpaired
    assert columns(unpaired)[1:] == ("₱3", "₱11"), unpaired
    form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', coins_out_form(unpaired)))
    response = request(opener, "/admin/vendos/coins-out", "POST", form)
    assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/vendos", response.status
    last, current, total = columns(vendo_row(response.read().decode(), vendo.id))
    assert last.startswith("Last Coins Out ₱3 · ") and (current, total) == ("₱0", "₱11"), (last, current, total)
    report.snapshot("vendo-coins-out-unpaired-row", address, password, opener, row=(last, current, total))


def vendo_list_online_devices_up_since(opener, report, address, password, client_address):
    """Online Devices (server) and Up since (#92): a Device online on one Vendo's
    HotSpot server counts on that row only, and a new boot nonce resets Up since."""
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        vendos = {}
        for vlan, number in ((10, 1), (20, 2)):
            board = pair_board(opener, client_address, password, fixture.board(vlan, number))
            paired.append(board)
            vendos[vlan] = SimulatedVendo(BASE_URL, board["id"], bytes.fromhex(board["secret"]), timeout=HTTP_TIMEOUT)
            assert vendos[vlan].heartbeat()[0] == 200

        page = request(opener, "/admin/vendos").read().decode()
        assert "<dt>Online Devices (server)</dt>" in page, page

        def detail(vendo):
            return vendo_row(request(opener, "/admin/vendos").read().decode(), vendo.id)

        def online_devices(vendo):
            return re.search(r'<dt>Online Devices \(server\)</dt><dd>(.*?)</dd>', detail(vendo), re.S).group(1)

        def up_since(vendo):
            """The detail's Up since, and the ledger's booted_at behind it."""
            status = detail(vendo)
            match = re.search(r"Up since (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)", status)
            assert match, status
            stored = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)
            return match.group(1), stored["booted_at"]

        assert (online_devices(vendos[10]), online_devices(vendos[20])) == ("0", "0")

        # A phone Device on VLAN 10 buys time from that server's Vendo and goes online.
        vendo = vendos[10]
        opened = portal_state(pause_device_fetch(client_address, password, ips[10], "/portal/api/coins/open", {}))
        window_id = opened["coin_window"]["id"]
        assert opened["coin_window"]["vendo_id"] == vendo.id, opened
        assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
        vendo.insert(1)
        assert vendo.heartbeat()[0] == 200
        pause_device_fetch(client_address, password, ips[10], "/portal/api/coins/done", {"window_id": window_id})
        assert vendo.heartbeat()[0] == 200
        assert vendo.heartbeat()[0] == 200
        coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
        deadline = time.monotonic() + 20
        while online_devices(vendo) != "1":
            assert time.monotonic() < deadline, detail(vendo)
            time.sleep(0.5)
        assert online_devices(vendos[20]) == "0", detail(vendos[20])
        report.snapshot("vendo-list-online-devices-per-server", address, password, opener,
                        online={vlan: online_devices(v) for vlan, v in vendos.items()})

        # Heartbeats from the same boot keep Up since; a new boot nonce resets it.
        assert vendos[20].heartbeat()[0] == 200
        other = up_since(vendos[20])
        booted = up_since(vendo)
        time.sleep(1.1)  # Up since shows whole seconds
        assert vendo.heartbeat()[0] == 200
        assert up_since(vendo) == booted
        vendo.nonce, vendo.rseq = (vendo.nonce + 1) & 0xFFFFFFFF, 0
        assert vendo.heartbeat()[0] == 200
        rebooted = up_since(vendo)
        # The page's "YYYY-MM-DD HH:MM:SS UTC" sorts as text; booted_at only has to change.
        assert rebooted[0] > booted[0] and rebooted[1] != booted[1], (booted, rebooted)
        assert vendos[20].heartbeat()[0] == 200
        assert up_since(vendos[20]) == other
        report.snapshot("vendo-list-up-since-resets-on-boot", address, password, opener, booted=booted, rebooted=rebooted)
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()


def vendo_list_rate_tier_validity(opener, report, address, password, client_address, device_ip):
    """#94: each coin Top-up takes Validity and Pause Limit from its Rate tier;
    Validity never shortens, the latest Top-up sets the Pause Limit, Vouchers
    use the site values, and a live Coin Window keeps its rate snapshot."""
    day = 86400
    # Tiers saved before #94 migrate with the site values (30 days, Pause Limit 3).
    migrated = json.load(request(opener, "/admin/state"))["rate_tiers"]
    assert migrated == [{"pesos": 1, "seconds": 60, "validity_seconds": 30 * day, "pause_limit": 3}], migrated
    for invalid in ("1=60,3600,1001", "1=60,0,1", "1=60,soon,1", "1=60,3600"):
        assert request(opener, "/admin/rates", "POST", {"tiers": invalid}).status == 400, invalid
    assert request(opener, "/admin/rates", "POST",
                   {"tiers": "1=60,3600,1\n30=86460,86400,15\n50=200000,no limit,20"}).status == 200
    page = request(opener, "/admin/rates").read().decode()
    # The Shop editor is seeded with each tier's Validity and Pause Limit; "no limit" for the last.
    assert "Expiry" not in page and 'data-cost="1" data-seconds="60" data-validity="3600" data-pause="1"' in page, page
    assert 'data-cost="50" data-seconds="200000" data-validity="no limit" data-pause="20"' in page, page
    tiers = json.load(request(opener, "/admin/state"))["rate_tiers"]
    assert tiers[2]["validity_seconds"] == 36500 * day, tiers  # "No limit" is the ledger's maximum Validity

    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)

    def fetch(path, form=None, cookie=""):
        return pause_device_fetch(client_address, password, device_ip, path, form, cookie)

    def open_window(cookie=""):
        opened = fetch("/portal/api/coins/open", {"vendo_id": paired["id"]}, cookie)
        assert vendo.heartbeat()[0] == 200 and vendo.window_id
        found = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", opened.get("http-headers", ""), re.I)
        return vendo.window_id, found.group(1) if found else cookie

    def settle(window_id, pesos, cookie):
        vendo.insert(pesos)
        assert vendo.heartbeat()[0] == 200
        fetch("/portal/api/coins/done", {"window_id": window_id}, cookie)
        assert vendo.heartbeat()[0] == 200 and vendo.closing
        assert vendo.heartbeat()[0] == 200
        state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
        deadline = time.monotonic() + 20
        while any(i["pending"] for i in state["credit_intents"] if i["time_credit_id"] == window["time_credit_id"]):
            assert time.monotonic() < deadline, "coin Top-up was not projected to the Router"
            time.sleep(0.2)
            state = json.load(request(opener, "/admin/state"))
        return window, next(v for v in state["validity"] if v["time_credit_id"] == window["time_credit_id"])

    # ₱30: one day of Validity and 15 Pauses.
    window_id, cookie = open_window()
    started = time.time()
    window, validity = settle(window_id, 30, cookie)
    assert window["credited_seconds"] == 86460
    assert started + day - 2 <= validity["valid_until"] <= time.time() + day + 2, validity
    long_deadline = validity["valid_until"]
    portal = portal_state(fetch("/portal/api/state", cookie=cookie))["time_credit"]
    assert portal["pause_limit"] == 15 and portal["pauses_left"] == 15, portal

    # ₱1 priced by a window opened before the rates change: the snapshot holds.
    window_id, cookie = open_window(cookie)
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=10,60,2"}).status == 200
    window, validity = settle(window_id, 1, cookie)
    assert window["credited_seconds"] == 60, "editing rates changed a live Coin Window's price"
    assert validity["valid_until"] == long_deadline, "a shorter ₱1 Validity cut the ₱30 deadline"
    portal = portal_state(fetch("/portal/api/state", cookie=cookie))["time_credit"]
    assert portal["pause_limit"] == 1 and portal["pauses_left"] == 1, portal
    state = portal_state(fetch("/portal/api/pause", {}, cookie))
    assert_message(state, "success", "paused")
    assert_message(portal_state(fetch("/portal/api/resume", {}, cookie)), "success", "online")
    try:
        fetch("/portal/api/pause", {}, cookie)
    except urllib.error.HTTPError as error:
        assert error.code == 400  # RouterOS wraps the Portal's 409 Pause Limit refusal.
    else:
        raise AssertionError("the ₱1 tier's Pause Limit of 1 was not enforced")
    report.snapshot("rate-tier-validity-never-shortens-and-latest-sets-pause-limit", address, password, opener,
                    long_deadline=long_deadline, pause_limit_after_one_peso=1, snapshot_credited_seconds=60)

    # A Voucher uses the site values: a shorter site Validity never shortens, and
    # the site Pause Limit replaces the tier's.
    assert request(opener, "/admin/validity", "POST", {"value": "60", "unit": "seconds"}).status == 200
    assert request(opener, "/admin/pause", "POST", {
        "pause_limit": "4", "idle_seconds": "120", "idle_enabled": "off"}).status == 200
    response = generate_vouchers(opener, {"seconds": "30", "count": "1"})
    code = voucher_codes(opener, response)[0]
    portal = portal_state(fetch("/portal/api/redeem", {"code": code}, cookie))["time_credit"]
    assert portal["valid_until"] == long_deadline, "a Voucher shortened Validity"
    assert portal["pause_limit"] == 4 and portal["pauses_left"] == 4, portal
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200
    assert request(opener, "/admin/pause", "POST", {
        "pause_limit": "3", "idle_seconds": "120", "idle_enabled": "off"}).status == 200
    # The Voucher stored the site Pause Limit of its Top-up; a later site change leaves it.
    portal = portal_state(fetch("/portal/api/state", cookie=cookie))["time_credit"]
    assert portal["pause_limit"] == 4 and portal["pauses_left"] == 4, portal
    assert request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]}).status == 204
    report.snapshot("voucher-keeps-validity-and-sets-site-pause-limit", address, password, opener,
                    long_deadline=long_deadline, site_pause_limit=4)


def vendo_list_hotspot_profile(opener, report, address, password, client_address, device_ip):
    """HotSpot profile per Vendo (#93): coins put the Time Credit on the Controller's
    copy of the Vendo's Router profile; Idle Pause keeps it in that family; a
    Voucher leaves it; a deleted P keeps its copies with a warning; an unused
    copy pair is removed on the reconciler pass."""
    router = lambda path, method="GET", body=None: router_rest(address, "admin", password, path, method, body)
    plan, copies = "e2e-plan", ("openpiso-e2e-plan-idle", "openpiso-e2e-plan-no-idle")

    def profiles():
        return {p["name"]: p for p in router("/ip/hotspot/user/profile")}

    def remove_profiles(names):
        for name, profile in profiles().items():
            if name in names:
                router("/ip/hotspot/user/profile/" + profile[".id"], "DELETE")

    def user_profile(time_credit_id):
        return router("/ip/hotspot/user?name=op-" + time_credit_id)[0]["profile"]

    def edit(profile, status=200):
        stored = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == paired["id"])
        form = {"id": paired["id"], "name": stored["name"], "location": stored["location"],
                "hotspot_server": stored["hotspot_server"], "hotspot_profile": profile}
        assert request(opener, "/admin/vendos/edit", "POST", form).status == status, (profile, status)

    def settle(pesos):
        """Coins on this Vendo for the Device, projected to the Router."""
        pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]})
        assert vendo.heartbeat()[0] == 200 and vendo.window_id
        window_id = vendo.window_id
        vendo.insert(pesos)
        assert vendo.heartbeat()[0] == 200
        pause_device_fetch(client_address, password, device_ip, "/portal/api/coins/done", {"window_id": window_id})
        assert vendo.heartbeat()[0] == 200 and vendo.closing
        assert vendo.heartbeat()[0] == 200
        state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
        deadline = time.monotonic() + 20
        while any(i["pending"] for i in state["credit_intents"] if i["time_credit_id"] == window["time_credit_id"]):
            assert time.monotonic() < deadline, "coin Top-up was not projected to the Router"
            time.sleep(0.2)
            state = json.load(request(opener, "/admin/state"))
        return window["time_credit_id"]

    def wait_online(time_credit_id):
        deadline = time.monotonic() + 20
        while not router("/ip/hotspot/active?user=op-" + time_credit_id):
            assert time.monotonic() < deadline, "the Device is not online"
            time.sleep(0.5)

    remove_profiles({plan, *copies})
    try:
        # P is the Operator's: a rate limit to copy, and fields the copy must never take.
        router("/ip/hotspot/user/profile", "PUT", {"name": plan, "rate-limit": "2M/4M", "shared-users": "5",
                                                  "session-timeout": "1h", "on-login": ":log info e2e-plan"})
        paired = pair_board(opener, client_address, password, coin_board(client_address, password))
        vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)
        assert vendo.heartbeat()[0] == 200

        # Details lists the Router's profiles, never the Controller's own; empty means the base profiles.
        row = vendo_row(request(opener, "/admin/vendos").read().decode(), paired["id"])
        select = re.search(r'<select name="hotspot_profile".*?</select>', row, re.S).group(0)
        choices = ["" if label == "Base profiles" else value or label for value, label in re.findall(r'<option(?: value="([^"]*)")?(?: selected)?>([^<]+)</option>', select)]
        assert "" in choices and plan in choices and "default" in choices, choices
        assert not any(c.startswith("openpiso-") for c in choices), choices
        for rejected in ("openpiso-idle", "missing-profile"):
            edit(rejected, 400)
        edit(plan)
        stored = next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == paired["id"])
        assert stored["hotspot_profile"] == plan, stored
        row = vendo_row(request(opener, "/admin/vendos").read().decode(), paired["id"])
        assert f'<dt>Profile</dt><dd>{plan}</dd>' in row, row
        copied = profiles()
        for name in copies:
            copy = copied[name]
            assert copy["rate-limit"] == "2M/4M" and copy["shared-users"] == "1", copy
            assert copy.get("session-timeout") != "1h" and not copy.get("on-login"), copy
        assert copied[copies[1]]["idle-timeout"] == "none"
        assert duration_seconds(copied[copies[0]]["idle-timeout"]) == 120, copied[copies[0]]
        # Saving the Vendo re-copies P after the Operator edits it on the Router.
        router("/ip/hotspot/user/profile/" + copied[plan][".id"], "PATCH", {"rate-limit": "3M/6M"})
        edit(plan)
        assert all(profiles()[name]["rate-limit"] == "3M/6M" for name in copies)

        # Coins on this Vendo put the Time Credit on the copy, with the site idle timeout.
        time_credit_id = settle(1)
        assert user_profile(time_credit_id) == copies[1], user_profile(time_credit_id)
        wait_online(time_credit_id)
        report.snapshot("hotspot-profile-coins-use-the-copy", address, password, opener,
                        time_credit_id=time_credit_id, profile=copies[1], copies={n: profiles()[n] for n in copies})

        # Idle Pause on and off moves the user only within its family.
        settings = {"pause_limit": "3", "idle_seconds": "120"}
        assert request(opener, "/admin/pause", "POST", {**settings, "idle_enabled": "on"}).status == 200
        assert user_profile(time_credit_id) == copies[0], user_profile(time_credit_id)
        assert request(opener, "/admin/pause", "POST", {**settings, "idle_enabled": "off"}).status == 200
        assert user_profile(time_credit_id) == copies[1], user_profile(time_credit_id)

        # A Voucher Top-up leaves the profile; so does changing the Vendo's Profile (future coins only).
        response = generate_vouchers(opener, {"seconds": "30", "count": "1"})
        code = voucher_codes(opener, response)[0]
        portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": code}))
        assert user_profile(time_credit_id) == copies[1], "a Voucher changed the HotSpot profile"
        report.snapshot("hotspot-profile-idle-pause-and-voucher-keep-the-family", address, password, opener,
                        time_credit_id=time_credit_id)

        # P deleted on the Router: the copies keep the Device online, and Details warns.
        remove_profiles({plan})
        time.sleep(12)  # one reconciler pass
        assert set(copies) <= set(profiles()), "a copy of a deleted P was removed while in use"
        wait_online(time_credit_id)
        row = vendo_row(request(opener, "/admin/vendos").read().decode(), paired["id"])
        assert f"Profile {plan} no longer exists on the Router" in row, row
        edit(plan)  # an unchanged, deleted P still saves
        report.snapshot("hotspot-profile-deleted-on-router-keeps-copies", address, password, opener,
                        time_credit_id=time_credit_id)

        # The Vendo moves to the base profiles: the user stays on its copy until its next coin Top-up.
        edit("")
        assert user_profile(time_credit_id) == copies[1]
        settle(1)
        assert user_profile(time_credit_id) == "openpiso-no-idle", user_profile(time_credit_id)
        deadline = time.monotonic() + 25
        while set(copies) & set(profiles()):
            assert time.monotonic() < deadline, "the unused copy pair was not removed"
            time.sleep(1)
        assert "openpiso-idle" in profiles() and "openpiso-no-idle" in profiles()
        report.snapshot("hotspot-profile-unused-copy-pair-removed", address, password, opener,
                        time_credit_id=time_credit_id)
        assert request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]}).status == 204
    finally:
        # A failed run may leave a Time Credit on a copy; move it off so the copy can go.
        for user in router("/ip/hotspot/user"):
            if user.get("profile") in copies:
                router("/ip/hotspot/user/" + user[".id"], "PATCH", {"profile": "openpiso-no-idle"})
        remove_profiles({plan, *copies})


def vendo_list_points(opener, report, address, password, client_address, device_ip):
    """#96: coins earn Points (thousandths) that survive Validity: they follow a
    Rebind, carry to the replacement Time Credit, and keep an expired Resume
    Code usable. Vouchers earn none. Leaves the site rates and Validity as found."""
    site = json.load(request(opener, "/admin/state"))
    saved_tiers = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]}'
                            for t in site["rate_tiers"])

    def fetch(ip, path, form=None, cookie=""):
        return pause_device_fetch(client_address, password, ip, path, form, cookie)

    def refused(ip, path, form, status):
        try:
            fetch(ip, path, form)
        except urllib.error.HTTPError as error:
            body = json.load(error)
            assert status in body.get("detail", ""), body
            return body
        raise AssertionError(f"{path} was not refused with {status}")

    def ledger_state():
        state = json.load(request(opener, "/admin/state"))
        return state, {tc["id"]: tc for tc in state["purchases"]["time_credits"]}

    def journal(state, time_credit_id):
        return [(row["reason"], row["delta_milli"]) for row in state["purchases"]["points_journal"]
                if row["time_credit_id"] == time_credit_id]

    # Move any live Time Credit off lan1, so this Device starts with none.
    second_device_up(client_address, password)
    second_ip = rebind_wait_device(client_address, password, "lan2")
    held = portal_state(fetch(device_ip, "/portal/api/state"))["time_credit"]
    if held:
        assert_message(portal_state(fetch(second_ip, "/portal/api/recover", {"code": held["resume_code"]})), "success", "online")
    assert portal_state(fetch(device_ip, "/portal/api/state"))["time_credit"] is None

    # Points are typed as decimals and stored in thousandths, never as floats.
    for invalid in ("1=10,90,3,0.0001", "1=10,90,3,-1", "1=10,90,3,lots", "1=10,90,3,0.1,9"):
        assert request(opener, "/admin/rates", "POST", {"tiers": invalid}).status == 400, invalid
    # A re-priced window keeps what it earned, so a higher tier may never give
    # fewer Points per peso (0.2 per ₱1, then 0.1 per ₱1): site and Vendo rates alike.
    decreasing = "1=10,90,3,0.2\n5=60,90,3,0.5"
    response = request(opener, "/admin/rates", "POST", {"tiers": decreasing})
    assert response.status == 400 and "fewer Points per peso" in response.read().decode(), decreasing
    # KiTifi's 0.1 per ₱1; the ₱5 tier earns 1 Point (0.2 per ₱1).
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=10,90,3,0.1\n5=60,90,3,1"}).status == 200
    tiers = json.load(request(opener, "/admin/state"))["rate_tiers"]
    assert [t["points_milli"] for t in tiers] == [100, 1000], tiers
    assert [(t["validity_seconds"], t["pause_limit"]) for t in tiers] == [(90, 3), (90, 3)], tiers
    page = request(opener, "/admin/rates").read().decode()
    assert "Points" in page and "0.1" in page, page

    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)

    # ₱4 earns 0.4 Points. A late fifth coin re-prices the window at the ₱5 tier
    # (1 Point in all) and adds only the 0.6 difference.
    opened = fetch(device_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]})
    cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", opened.get("http-headers", ""), re.I).group(1)
    assert vendo.heartbeat()[0] == 200 and vendo.window_id
    window_id = vendo.window_id
    vendo.insert(4)
    assert vendo.heartbeat()[0] == 200
    fetch(device_ip, "/portal/api/coins/done", {"window_id": window_id}, cookie)
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    state, window = coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
    earner = window["time_credit_id"]
    assert window["credited_seconds"] == 40
    assert ledger_state()[1][earner]["points_milli"] == 400
    vendo.total += 1  # a pulse accepted before Close, delivered late
    assert vendo.post(vendo.frame(5))[0] == 200
    state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    assert window["coins"] == 5 and window["credited_seconds"] == 60
    state, credits = ledger_state()
    assert credits[earner]["points_milli"] == 1000, credits[earner]
    assert journal(state, earner) == [("coin-earn", 400), ("coin-earn", 600)], journal(state, earner)
    portal = portal_state(fetch(device_ip, "/portal/api/state", cookie=cookie))["time_credit"]
    assert portal["points_milli"] == 1000, portal
    assert "Points" in request(opener, "/admin/time-credits?tc=" + earner).read().decode()
    report.snapshot("points-earn-and-late-coin-adds-difference", address, password, opener,
                    time_credit_id=earner, points_milli=1000, journal=journal(state, earner))

    # A Rebind to a new MAC keeps the balance.
    code = portal["resume_code"]
    moved = portal_state(fetch(second_ip, "/portal/api/recover", {"code": code}))
    assert_message(moved, "success", "online")
    assert moved["time_credit"]["points_milli"] == 1000, moved
    state, credits = ledger_state()
    assert credits[earner]["ip"] == second_ip and credits[earner]["points_milli"] == 1000, credits[earner]

    # A Voucher earns no Points. A 10-second site Validity cannot shorten the tier's.
    assert request(opener, "/admin/validity", "POST", {"value": "10", "unit": "seconds"}).status == 200
    response = generate_vouchers(opener, {"seconds": "30", "count": "1"})
    voucher = voucher_codes(opener, response)[0]
    redeemed = portal_state(fetch(second_ip, "/portal/api/redeem", {"code": voucher}))
    assert redeemed["time_credit"]["points_milli"] == 1000, redeemed
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200
    state, credits = ledger_state()
    assert credits[earner]["points_milli"] == 1000 and len(journal(state, earner)) == 2
    report.snapshot("points-follow-rebind-and-voucher-earns-none", address, password, opener,
                    time_credit_id=earner, rebound_ip=second_ip, points_milli=1000)

    # After Validity the Points stay: the Portal says so, and the expired
    # Resume Code Rebinds the Time Credit (back to lan1) without logging it in.
    deadline = next(v for v in state["validity"] if v["time_credit_id"] == earner)["valid_until"]
    wait_for_validity_removal(opener, address, password, earner, deadline)
    kept = portal_state(fetch(second_ip, "/portal/api/state"))
    assert kept["time_credit"]["expired"] and kept["time_credit"]["points_milli"] == 1000, kept
    assert_message(kept, "warning", "points-kept")
    recovered = portal_state(fetch(device_ip, "/portal/api/recover", {"code": code}))
    assert_message(recovered, "warning", "points-kept")
    assert "1 Points are kept" in recovered["message"]["text"], recovered
    state, credits = ledger_state()
    assert credits[earner]["ip"] == device_ip, credits[earner]
    assert not router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + earner), "expired Time Credit revived on the Router"
    assert not any(i["pending"] for i in state["credit_intents"] if i["time_credit_id"] == earner)

    # Opening a Coin Window creates the replacement Time Credit and carries the
    # balance at once, so a window abandoned without coins strands nothing.
    fetch(device_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]})
    assert vendo.heartbeat()[0] == 200 and vendo.window_id
    window_id = vendo.window_id
    fetch(device_ip, "/portal/api/coins/done", {"window_id": window_id})
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    assert vendo.heartbeat()[0] == 200
    state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    replacement = window["time_credit_id"]
    assert replacement != earner and window["coins"] == 0 and window["credited_seconds"] == 0, window
    state, credits = ledger_state()
    assert credits[earner]["points_milli"] == 0 and credits[replacement]["points_milli"] == 1000, credits
    assert journal(state, earner)[-1] == ("carry-out", -1000), journal(state, earner)
    assert journal(state, replacement) == [("carry-in", 1000)], journal(state, replacement)
    # A Voucher on that Device tops up the replacement and keeps the balance.
    response = generate_vouchers(opener, {"seconds": "30", "count": "1"})
    voucher = voucher_codes(opener, response)[0]
    redeemed = portal_state(fetch(device_ip, "/portal/api/redeem", {"code": voucher}))["time_credit"]
    assert redeemed["points_milli"] == 1000 and redeemed["bought"] == 30, redeemed
    state, credits = ledger_state()
    assert credits[replacement]["points_milli"] == 1000 and journal(state, replacement) == [("carry-in", 1000)], credits
    report.snapshot("points-carry-when-window-opens-on-expired-time-credit", address, password, opener,
                    expired_id=earner, replacement_id=replacement, carried_milli=1000, voucher_bought=30)

    # New coins top up the replacement, which already holds the carried balance.
    fetch(device_ip, "/portal/api/coins/open", {"vendo_id": paired["id"]})
    assert vendo.heartbeat()[0] == 200 and vendo.window_id
    window_id = vendo.window_id
    vendo.insert(1)
    assert vendo.heartbeat()[0] == 200
    fetch(device_ip, "/portal/api/coins/done", {"window_id": window_id})
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    assert vendo.heartbeat()[0] == 200
    state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    assert window["time_credit_id"] == replacement and window["credited_seconds"] == 10, window
    state, credits = ledger_state()
    assert credits[earner]["points_milli"] == 0 and credits[replacement]["points_milli"] == 1100, credits
    assert journal(state, earner)[-1] == ("carry-out", -1000), journal(state, earner)
    assert sorted(journal(state, replacement)) == [("carry-in", 1000), ("coin-earn", 100)], journal(state, replacement)
    assert next(v for v in state["validity"] if v["time_credit_id"] == earner)["expired_at"], "expired Time Credit revived"
    report.snapshot("points-carry-to-replacement-time-credit", address, password, opener,
                    expired_id=earner, replacement_id=replacement, carried_milli=1000, replacement_points_milli=1100)

    # Without Points, an expired Resume Code is refused as expired.
    body = refused(device_ip, "/portal/api/recover", {"code": code}, "403")
    report.snapshot("expired-resume-code-without-points-refused", address, password, opener, refusal=body)

    assert request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]}).status == 204
    assert request(opener, "/admin/rates", "POST", {"tiers": saved_tiers}).status == 200


def vendo_list_points_redeem(opener, report, address, password, client_address, device_ip):
    """#97: the site Points table buys time on the Portal. A redemption is a
    Top-up with the row's Validity and Pause Limit, never Sales, and leaves the
    HotSpot profile alone; on an expired Time Credit it first takes the
    replacement path. Leaves the site rates and Points table as found."""
    site = json.load(request(opener, "/admin/state"))
    saved_tiers = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]}'
                            for t in site["rate_tiers"])
    assert site["points_tiers"] == [], site["points_tiers"]  # no Points table until the Operator sets one

    def fetch(path, form=None, cookie=""):
        return pause_device_fetch(client_address, password, device_ip, path, form, cookie)

    def ledger_state():
        state = json.load(request(opener, "/admin/state"))
        return state, {tc["id"]: tc for tc in state["purchases"]["time_credits"]}

    def journal(state, time_credit_id):
        return [(row["reason"], row["delta_milli"]) for row in state["purchases"]["points_journal"]
                if row["time_credit_id"] == time_credit_id]

    def projected(state, time_credit_id):
        deadline = time.monotonic() + 20
        while any(i["pending"] for i in state["credit_intents"] if i["time_credit_id"] == time_credit_id):
            assert time.monotonic() < deadline, "Points Top-up was not projected to the Router"
            time.sleep(0.2)
            state = json.load(request(opener, "/admin/state"))
        return router_rest(address, "admin", password, "/ip/hotspot/user?name=op-" + time_credit_id)[0]

    def redeem(points_milli, seconds, cookie=""):
        return portal_state(fetch("/portal/api/points/redeem", {"points_milli": points_milli, "seconds": seconds}, cookie))

    def too_low(points_milli, seconds):
        try:
            fetch("/portal/api/points/redeem", {"points_milli": points_milli, "seconds": seconds})
        except urllib.error.HTTPError as error:
            detail = json.load(error).get("detail", "")
            assert "409" in detail, detail  # /tool/fetch reports only the status
            return detail
        raise AssertionError("a redemption above the balance was accepted")

    # The Points table has its own admin page: points=seconds,validity,pause limit,
    # with fractional costs in thousandths.
    for invalid in ("5=1800", "0=1800,3600,2", "0.0001=60,3600,1", "5=0,3600,2", "5=1800,3600,1001", "5=1800,soon,2"):
        assert request(opener, "/admin/points", "POST", {"tiers": invalid}).status == 400, invalid
    assert request(opener, "/admin/points", "POST", {"tiers": "0.5=60,3600,1\n5=1800,3600,2"}).status == 200
    tiers = json.load(request(opener, "/admin/state"))["points_tiers"]
    assert tiers == [{"points_milli": 500, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1},
                     {"points_milli": 5000, "seconds": 1800, "validity_seconds": 3600, "pause_limit": 2}], tiers
    page = request(opener, "/admin/points").read().decode()
    assert "Points table" in page and 'data-cost="0.5" data-seconds="60" data-validity="3600"' in page, page
    # 1 Point per ₱1, with a short Validity so this Time Credit expires soon.
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=10,20,3,1"}).status == 200

    paired = pair_board(opener, client_address, password, coin_board(client_address, password))
    vendo = SimulatedVendo(BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=HTTP_TIMEOUT)

    # Move a live Time Credit off lan1, so the coins below buy one that expires
    # after the tier's 20 seconds rather than topping up a long-lived one.
    held = portal_state(fetch("/portal/api/state"))["time_credit"]
    if held and not held["expired"]:
        second_device_up(client_address, password)
        second_ip = rebind_wait_device(client_address, password, "lan2")
        moved = pause_device_fetch(client_address, password, second_ip, "/portal/api/recover", {"code": held["resume_code"]})
        assert_message(portal_state(moved), "success", "online")

    # ₱11 earns 11 Points on top of whatever this Device already holds.
    held = portal_state(fetch("/portal/api/state"))["time_credit"]
    start_milli = held["points_milli"] if held else 0
    assert start_milli < 4000, held  # so two 5-Point redemptions leave too little for a third
    opened = fetch("/portal/api/coins/open", {"vendo_id": paired["id"]})
    found = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", opened.get("http-headers", ""), re.I)
    cookie = found.group(1) if found else ""
    assert vendo.heartbeat()[0] == 200 and vendo.window_id
    window_id = vendo.window_id
    vendo.insert(11)
    assert vendo.heartbeat()[0] == 200
    fetch("/portal/api/coins/done", {"window_id": window_id}, cookie)
    assert vendo.heartbeat()[0] == 200 and vendo.closing
    assert vendo.heartbeat()[0] == 200
    state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
    expired_id = window["time_credit_id"]
    balance = start_milli + 11000
    assert ledger_state()[1][expired_id]["points_milli"] == balance

    # The Portal offers the site Points table beside the balance.
    portal = portal_state(fetch("/portal/api/state", cookie=cookie))
    assert portal["points"] == tiers and portal["time_credit"]["points_milli"] == balance, portal

    # After Validity the Portal says Points can still be redeemed.
    deadline = next(v for v in state["validity"] if v["time_credit_id"] == expired_id)["valid_until"]
    wait_for_validity_removal(opener, address, password, expired_id, deadline)
    kept = portal_state(fetch("/portal/api/state", cookie=cookie))
    assert_message(kept, "warning", "points-kept")
    assert "Insert coins or redeem Points" in kept["message"]["text"], kept
    sales_before = sales(opener)

    # Redeeming on the expired Time Credit creates a replacement, carries the
    # balance to it, then deducts and credits the row's 30 minutes.
    started = time.time()
    replaced = redeem(5000, 1800, cookie)
    assert_message(replaced, "success", "online")
    assert replaced["time_credit"]["points_milli"] == balance - 5000, replaced
    assert replaced["time_credit"]["bought"] == 1800 and replaced["time_credit"]["pause_limit"] == 2, replaced
    state, credits = ledger_state()
    replacement = next(row["ref"] for row in state["purchases"]["points_journal"]
                       if row["time_credit_id"] == expired_id and row["reason"] == "carry-out")
    assert replacement != expired_id and credits[replacement]["ip"] == device_ip, credits[replacement]
    assert credits[expired_id]["points_milli"] == 0 and credits[replacement]["points_milli"] == balance - 5000, credits
    assert journal(state, expired_id)[-1] == ("carry-out", -balance), journal(state, expired_id)
    assert journal(state, replacement) == [("carry-in", balance), ("redeem", -5000)], journal(state, replacement)
    assert next(v for v in state["validity"] if v["time_credit_id"] == expired_id)["expired_at"], "expired Time Credit revived"
    validity = next(v for v in state["validity"] if v["time_credit_id"] == replacement)
    assert started + 3600 - 2 <= validity["valid_until"] <= time.time() + 3600 + 2, validity
    audit = [a for a in state["purchases"]["audit"] if a["time_credit_id"] == replacement]
    assert [(a["reason"], a["before_seconds"], a["after_seconds"]) for a in audit] == [("points-redemption", 0, 1800)], audit
    user = projected(state, replacement)
    assert sales(opener) == sales_before, "a Points redemption counted as Sales"
    report.snapshot("points-redeem-on-expired-time-credit-creates-replacement", address, password, opener,
                    expired_id=expired_id, replacement_id=replacement, carried_milli=balance, redeemed_milli=5000)

    # Redeeming 5 Points on a live Time Credit is a 30-minute Top-up: the
    # balance drops, Sales and the HotSpot profile are unchanged.
    topped = redeem(5000, 1800)
    assert_message(topped, "success", "online")
    assert topped["time_credit"]["bought"] == 3600, topped
    state, credits = ledger_state()
    assert credits[replacement]["points_milli"] == balance - 10000, credits[replacement]
    assert journal(state, replacement)[-1] == ("redeem", -5000), journal(state, replacement)
    after = projected(state, replacement)
    assert after["limit-uptime"] != user["limit-uptime"] and after["profile"] == user["profile"], (user, after)
    assert sales(opener) == sales_before, "a Points redemption counted as Sales"
    assert "Points" in request(opener, "/admin/time-credits?tc=" + replacement).read().decode()
    report.snapshot("points-redeem-thirty-minute-top-up-not-sales", address, password, opener,
                    time_credit_id=replacement, points_milli=balance - 10000, bought_seconds=3600, profile=after["profile"])

    # A balance below the row's cost is refused and changes nothing.
    detail = too_low(5000, 1800)
    state, credits = ledger_state()
    assert credits[replacement]["points_milli"] == balance - 10000 and credits[replacement]["bought_seconds"] == 3600
    assert journal(state, replacement)[-1] == ("redeem", -5000)
    report.snapshot("points-redeem-too-low-refused", address, password, opener, refusal=detail)

    assert request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]}).status == 204
    assert request(opener, "/admin/rates", "POST", {"tiers": saved_tiers}).status == 200
    assert request(opener, "/admin/points", "POST", {"tiers": ""}).status == 200


# Shop pages' coverage pills and Vendo lists (Rate tiers, Points table) share one markup contract.
def shop_group(body, name):
    """The group panel's markup: <div id="group-NAME" ... hidden>…</div>."""
    match = re.search(rf'<div[^>]*id="group-{name}"[^>]*>(.*?)</div>', body, re.S)
    assert match and "hidden" in re.search(rf'<div[^>]*id="group-{name}"[^>]*>', body).group(0), body
    return match.group(1)


def shop_pills(body, wordings, seen):
    """Each pill's count, checking the button wiring and that its wording agrees with the count.
    wordings is ((group, singular, plural), ...); each wording met is added to seen."""
    counts = {}
    for name, one, many in wordings:
        pill = re.search(rf'<button type="button" class="pill" data-group="{name}" aria-expanded="false" aria-controls="group-{name}">(.*?)</button>', body, re.S)
        assert pill, (name, body)
        words = re.search(r"<strong>(\d+)</strong> ([^<]+?)\s*(?:<svg|$)", pill.group(1), re.S)
        assert words, pill.group(1)
        count = int(words.group(1))
        assert words.group(2).strip() == (one if count == 1 else many), (count, words.group(2))
        seen.add(words.group(2).strip())
        counts[name] = count
    return counts


def shop_rows(body, name):
    """Each Vendo listed in the group as (id, name, sub line, link text, href)."""
    return [(int(i), html.unescape(n), re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip(), t, html.unescape(h))
            for i, n, s, h, t in re.findall(r'<li data-vendo="(\d+)">.*?<strong>([^<]*)</strong><span class="sub">(.*?)</span>.*?<a href="([^"]+)">([^<]+)</a></li>',
                                            shop_group(body, name), re.S)]


def shop_defaults_rate_tiers(opener, report, address, password, client_address):
    """Shop › Rate tiers (#104): the Shop sidebar group and palette entries, the
    page's breadcrumb, scope line, coverage counts and structured editor, and
    saves from the editor and from the old textarea form, both through the shared
    validator. Leaves the Shop Rate tiers as found."""
    found = json.load(request(opener, "/admin/state"))
    milli = lambda m: f"{m // 1000}.{m % 1000:03d}".rstrip("0").rstrip(".")
    saved_rates = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]},{milli(t.get("points_milli", 0))}'
                            for t in found["rate_tiers"])

    def page():
        response = request(opener, "/admin/rates")
        assert response.status == 200, response.status
        return response, response.read().decode()

    def counts(body):
        shop = re.search(r"<strong>(\d+)</strong> Vendos? uses? Shop tiers", body)
        custom = re.search(r"<strong>(\d+)</strong> Vendos? ha(?:ve|s) Custom tiers", body)
        assert shop and custom, body
        return int(shop.group(1)), int(custom.group(1))

    def expected_counts():
        """Paired Vendos (Deactivated included) split by whether they have their own tiers."""
        paired = [v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["status"] == "paired"]
        return sum(v["rate_tiers"] is None for v in paired), sum(v["rate_tiers"] is not None for v in paired)

    def stored():
        return json.load(request(opener, "/admin/state"))["rate_tiers"]

    # Nav: Shop sits directly below Vendo List and holds Rate tiers and Points
    # table; Coins keeps only Coin sales and Credit intents.
    response, body = page()
    main = re.search(r'<div class="group-label">Main</div>\s*<ul class="sb-menu">(.*?)</ul>\s*</div>', body, re.S).group(1)
    groups = re.findall(r'data-tooltip="([^"]+)"', main)
    assert groups[groups.index("Vendo List") + 1] == "Shop", groups
    item = lambda label: re.search(rf'<li class="menu-item"[^>]*>\s*<a[^>]*data-tooltip="{label}".*?</li>\s*</ul>\s*</li>', main, re.S).group(0)
    subs = lambda label: re.findall(r'<a class="menu-sub-btn" href="([^"]+)"[^>]*>([^<]+)</a>', item(label))
    assert subs("Shop") == [("/admin/rates", "Rate tiers"), ("/admin/points", "Points table")], subs("Shop")
    assert subs("Coins") == [("/admin/coins", "Coin sales"), ("/admin/intents", "Credit intents")], subs("Coins")
    # The lucide store icon: its awning-and-door outline.
    assert re.search(r'href="/admin/rates"[^>]*data-tooltip="Shop"[^>]*>\s*<svg[^>]*>.*?M15 22v-4a2 2 0 0 0-2-2h-2', item("Shop"), re.S), item("Shop")
    palette = json.loads(html.unescape(re.search(r'data-items="([^"]*)"', body).group(1)))
    for label, href in (("Rate tiers", "/admin/rates"), ("Points table", "/admin/points")):
        assert {"label": label, "hint": "Shop", "href": href} in palette, palette
        assert not any(entry["label"] == label and entry["hint"] == "Coins" for entry in palette), palette

    # Page text: breadcrumb, scope line, the structured editor in place of the textarea.
    crumbs = re.search(r'<nav id="crumbs"[^>]*>(.*?)</nav>', body, re.S).group(1)
    assert re.search(r"<li[^>]*>Shop</li>.*<li aria-current=\"page\">Rate tiers</li>", crumbs, re.S), crumbs
    assert "<title>Rate tiers" in body, body
    assert "Default Rate tiers for this Shop. Every Vendo uses them unless it has its own." in body, body
    assert re.search(r'<form\b[^>]*action="/admin/rates"[^>]*data-tiers="rates"', body), body
    assert "Save Shop tiers" in body and "Add tier" in body and "<textarea" not in body, body
    assert 'name="id"' not in re.search(r'<form\b[^>]*data-tiers="rates".*?</form>', body, re.S).group(0)
    for tier in found["rate_tiers"]:
        assert f'data-cost="{tier["pesos"]}" data-seconds="{tier["seconds"]}"' in body, (tier, body)
    # Admin CSP holds: no inline styles or scripts on the page.
    policy = {directive.split()[0]: directive.split()[1:]
              for directive in (response.headers["Content-Security-Policy"] or "").split(";") if directive.strip()}
    assert policy.get("script-src") == ["'self'"] and policy.get("style-src") == ["'self'"], policy
    assert not re.search(r"<style|\sstyle=|\son[a-z]+=|<script(?![^>]*\ssrc=)", body), "inline style or script"
    report.snapshot("shop-rate-tiers-nav-and-page", address, password, opener, nav=groups, palette=palette)

    # Counts: paired Vendos using Shop tiers vs having Custom tiers; Deactivated
    # Vendos still count, unpaired ones do not.
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        base_shop, base_custom = counts(page()[1])
        assert (base_shop, base_custom) == expected_counts()
        boards = [pair_board(opener, client_address, password, fixture.board(vlan, n)) for n, vlan in ((1, 10), (2, 20))]
        paired.extend(boards)
        first, second = (SimulatedVendo(BASE_URL, b["id"], bytes.fromhex(b["secret"]), timeout=HTTP_TIMEOUT) for b in boards)
        for vendo in (first, second):
            assert vendo.heartbeat()[0] == 200
        assert counts(page()[1]) == (base_shop + 2, base_custom) == expected_counts()
        assert request(opener, "/admin/vendos/rates", "POST", {"id": first.id, "action": "customize"}).status == 200
        assert counts(page()[1]) == (base_shop + 1, base_custom + 1) == expected_counts()
        assert request(opener, "/admin/vendos/active", "POST", {"id": second.id, "active": "0"}).status == 200
        assert counts(page()[1]) == (base_shop + 1, base_custom + 1) == expected_counts()
        report.snapshot("shop-rate-tiers-counts-paired-vendos", address, password, opener, counts=counts(page()[1]))
        assert request(opener, "/admin/vendos/unpair", "POST", {"id": first.id}).status == 204
        paired.remove(boards[0])
        assert counts(page()[1]) == (base_shop + 1, base_custom) == expected_counts()
        report.snapshot("shop-rate-tiers-counts-exclude-unpaired", address, password, opener, counts=counts(page()[1]))
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()

    try:
        def editor(rows):
            """The structured editor's post: repeated tier_* fields plus its hidden action=save."""
            form = [("action", "save")]
            for price, minutes, validity, unit, pause, points in rows:
                form += [("tier_price", price), ("tier_minutes", minutes), ("tier_validity", validity),
                         ("tier_unit", unit), ("tier_pause", pause), ("tier_points", points)]
            return request(opener, "/admin/rates", "POST", form)

        # Editor save: rows land as Shop tiers and reseed the editor.
        response = editor([("1", "2", "1", "hours", "1", "0"), ("5", "15", "1", "days", "3", "0.5")])
        assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/rates", response.status
        editor_tiers = [{"pesos": 1, "seconds": 120, "validity_seconds": 3600, "pause_limit": 1},
                        {"pesos": 5, "seconds": 900, "validity_seconds": 86400, "pause_limit": 3, "points_milli": 500}]
        assert stored() == editor_tiers, stored()
        body = page()[1]
        assert 'data-cost="5" data-seconds="900" data-validity="86400" data-pause="3" data-points="0.5"' in body, body
        report.snapshot("shop-rate-tiers-editor-save", address, password, opener)

        # Invalid sets are refused with the shared message and change nothing.
        shared = "start with a ₱1 tier; choose up to 100 tiers"
        response = editor([("2", "2", "1", "hours", "1", "0")])
        assert response.status == 400 and shared in response.read().decode(), response.status
        response = request(opener, "/admin/rates", "POST", {"tiers": "\n".join(f"{p}={p * 60}" for p in range(1, 102))})
        assert response.status == 400 and shared in response.read().decode(), response.status
        response = editor([(str(p), str(p), "1", "hours", "1", "0") for p in range(1, 102)])
        assert response.status == 400, response.status
        assert stored() == editor_tiers, stored()

        # The old textarea form still saves Shop tiers.
        response = request(opener, "/admin/rates", "POST", {"tiers": "1=60,3600,1\n10=900,86400,2,1"})
        assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/rates", response.status
        assert stored() == [{"pesos": 1, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1},
                            {"pesos": 10, "seconds": 900, "validity_seconds": 86400, "pause_limit": 2, "points_milli": 1000}], stored()
        assert 'data-cost="10" data-seconds="900" data-validity="86400" data-pause="2" data-points="1"' in page()[1]
        report.snapshot("shop-rate-tiers-textarea-save", address, password, opener)
    finally:
        assert request(opener, "/admin/rates", "POST", {"tiers": saved_rates}).status == 200


def shop_defaults_pill_lists(opener, report, address, password, client_address):
    """Shop › Rate tiers pill lists (#105): each pill's count agrees in number
    and its list holds the paired Vendos (Deactivated with status, unpaired
    left out) by name; Open and Edit tiers link into the Vendo List, Edit tiers
    with the tab query; in the Operator's browser a pill expands its list and
    collapses the other, from the mouse and the keyboard, and Edit tiers opens
    the Vendo dialog on Rate tiers. A Shop edit reprices an inheriting Vendo's
    next Coin Window, leaves a Custom Vendo's alone, and a window open during
    the edit keeps its snapshotted tiers through a late coin. Leaves the Shop
    tiers as found."""
    found = json.load(request(opener, "/admin/state"))
    milli = lambda m: f"{m // 1000}.{m % 1000:03d}".rstrip("0").rstrip(".")
    saved_rates = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]},{milli(t.get("points_milli", 0))}'
                            for t in found["rate_tiers"])
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    seen = set()  # pill wordings and empty-group sentences the scenario has met

    def page():
        response = request(opener, "/admin/rates")
        assert response.status == 200, response.status
        return response.read().decode()

    pill_wordings = (("shop", "Vendo uses Shop tiers", "Vendos use Shop tiers"),
                     ("custom", "Vendo has Custom tiers", "Vendos have Custom tiers"))

    def expected(online):
        """The lists from /admin/state: paired Vendos only, by display name, split by own tiers."""
        lists = {"shop": [], "custom": []}
        vendos = sorted((v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["status"] == "paired"),
                        key=lambda v: v["name"] or f'Vendo {v["id"]}')
        for v in vendos:
            name = v["name"] or f'Vendo {v["id"]}'
            status = "Deactivated" if not v["active"] else online.get(v["id"], "Offline")
            where = f'{v["location"]} · ' if v["location"] else ""
            if v["rate_tiers"] is None:
                lists["shop"].append((v["id"], name, where + status, "Open", f'/admin/vendos?vendo={v["id"]}'))
            else:
                count = len(v["rate_tiers"])
                lists["custom"].append((v["id"], name, f'{where}{status} · {count} {"tier" if count == 1 else "tiers"}',
                                        "Edit tiers", f'/admin/vendos?vendo={v["id"]}&tab=rates'))
        return lists

    def check(online):
        """The page's pills and lists match /admin/state; empty groups read as a sentence."""
        body = page()
        want = expected(online)
        assert shop_pills(body, pill_wordings, seen) == {name: len(items) for name, items in want.items()}, (shop_pills(body, pill_wordings, seen), want)
        for name, empty in (("shop", "No Vendo uses Shop tiers."), ("custom", "No Vendo has its own tiers.")):
            got = shop_rows(body, name)
            assert got == want[name], (name, got, want[name])
            if not want[name]:
                assert empty in shop_group(body, name), shop_group(body, name)
                seen.add(empty)
        assert "A change here applies to each from its next Coin Window." in shop_group(body, "shop")
        assert "Shop changes do not affect these." in shop_group(body, "custom")
        return body

    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        assert request(opener, "/admin/rates", "POST", {"tiers": "1=60,3600,1"}).status == 200

        def stored(vendo):
            return next(v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["id"] == vendo.id)

        def buy(vlan, vendo, pesos):
            """The VLAN's Device opens a Coin Window on vendo and pays: its credited seconds."""
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {"vendo_id": vendo.id}))
            window_id = opened["coin_window"]["id"]
            assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
            vendo.insert(pesos)
            assert vendo.heartbeat()[0] == 200
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/done", {"window_id": window_id})
            assert vendo.heartbeat()[0] == 200
            assert vendo.heartbeat()[0] == 200
            return coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")[1]["credited_seconds"]

        # Before pairing: an empty Custom group still shows its pill and a sentence.
        check({})
        boards = [pair_board(opener, client_address, password, fixture.board(vlan, n)) for n, vlan in ((1, 10), (2, 20))]
        paired.extend(boards)
        custom, follower = (SimulatedVendo(BASE_URL, b["id"], bytes.fromhex(b["secret"]), timeout=HTTP_TIMEOUT) for b in boards)
        for vendo in (custom, follower):
            assert vendo.heartbeat()[0] == 200
        online = {custom.id: "Online", follower.id: "Online"}
        # Named "Zeta stall" sorts after the unnamed "Vendo N"; its location shows on the Shop list.
        assert request(opener, "/admin/vendos/edit", "POST", {"id": custom.id, "name": "Zeta stall", "location": "Market",
                                                              "hotspot_server": "opnw-vlan10"}).status == 200
        body = check(online)
        assert [r[0] for r in shop_rows(body, "shop")][-1] == custom.id, shop_rows(body, "shop")
        report.snapshot("shop-pill-lists-follow-paired-vendos", address, password, opener, pills=shop_pills(body, pill_wordings, seen))

        # Customize moves the Vendo to the Custom list, linked to its Rate tiers tab.
        assert request(opener, "/admin/vendos/rates", "POST", {"id": custom.id, "action": "customize"}).status == 200
        assert request(opener, "/admin/vendos/rates", "POST", {"id": custom.id, "action": "save", "tiers": "1=120,7200,2"}).status == 200
        body = check(online)
        assert any(r[4] == f"/admin/vendos?vendo={custom.id}&tab=rates" for r in shop_rows(body, "custom")), shop_rows(body, "custom")
        # Deactivated stays listed, with its status.
        assert request(opener, "/admin/vendos/active", "POST", {"id": follower.id, "active": "0"}).status == 200
        body = check(online)
        assert any(r[0] == follower.id and r[2].endswith("Deactivated") for r in shop_rows(body, "shop")), shop_rows(body, "shop")
        assert request(opener, "/admin/vendos/active", "POST", {"id": follower.id, "active": "1"}).status == 200
        report.snapshot("shop-pill-lists-custom-and-deactivated", address, password, opener, pills=shop_pills(body, pill_wordings, seen))

        # The Operator's browser: pills toggle from mouse and keyboard; Edit tiers opens the Rate tiers tab.
        out = report.path.parent / "shop-pills"
        out.mkdir(parents=True, exist_ok=True)
        _browser_module.ensure_image()
        browser = _browser_module.run_browser("admin", ["--network", "host"], BASE_URL, "shop-pills",
                                              {"password": ADMIN_PASSWORD, "custom_id": custom.id, "follower_id": follower.id}, out, {}, report)
        report.data["shop_pills"] = {"screenshots": [f"shop-pills/{name}.png" for name in browser["screenshots"]], "browser": browser}
        report.snapshot("shop-pill-lists-toggle-and-deep-link", address, password, opener, browser=browser)

        # Pricing: the follower pays Shop tiers, the Custom Vendo its own.
        assert buy(20, follower, 1) == 60 and buy(10, custom, 1) == 120
        # A window open on the follower during a Shop edit keeps its snapshotted tiers, through a late coin.
        opened = portal_state(pause_device_fetch(client_address, password, ips[20], "/portal/api/coins/open", {"vendo_id": follower.id}))
        window_id = opened["coin_window"]["id"]
        assert follower.heartbeat()[0] == 200 and follower.window_id == window_id
        follower.insert(1)
        assert follower.heartbeat()[0] == 200
        assert request(opener, "/admin/rates", "POST", {"tiers": "1=90,3600,1"}).status == 200
        pause_device_fetch(client_address, password, ips[20], "/portal/api/coins/done", {"window_id": window_id})
        assert follower.heartbeat()[0] == 200 and follower.closing
        state, window = coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")
        assert window["coins"] == 1 and window["credited_seconds"] == 60 and window["closed_at"] == 0, window
        follower.total += 1  # a pulse accepted before Close but delivered late
        late = follower.frame(5)
        assert follower.post(late, ignore_ack=True)[0] == 200
        assert follower.post(late)[0] == 200
        state, window = coin_window_wait(opener, window_id, lambda w: w["closed_at"] != 0)
        assert window["coins"] == 2 and window["credited_seconds"] == 120, window
        assert follower.heartbeat()[0] == 200
        # The next windows: the follower at the edited Shop tiers, the Custom Vendo unchanged.
        assert buy(20, follower, 1) == 90 and buy(10, custom, 1) == 120
        assert stored(custom)["rate_tiers"] == [{"pesos": 1, "seconds": 120, "validity_seconds": 7200, "pause_limit": 2}]
        report.snapshot("shop-pill-lists-shop-edit-reprices-next-window", address, password, opener,
                        snapshot_credit=120, repriced_follower=90, custom_unchanged=120)

        # Unpaired Vendos leave both lists.
        assert request(opener, "/admin/vendos/unpair", "POST", {"id": follower.id}).status == 204
        paired.remove(boards[1])
        body = check(online)
        assert all(r[0] != follower.id for r in shop_rows(body, "shop") + shop_rows(body, "custom"))
        # Singular and plural wordings, and both empty sentences, were met on the way.
        for wording in ("Vendo uses Shop tiers", "Vendos use Shop tiers", "Vendo has Custom tiers",
                        "No Vendo uses Shop tiers.", "No Vendo has its own tiers."):
            assert wording in seen, (wording, seen)
        report.snapshot("shop-pill-lists-exclude-unpaired", address, password, opener, wordings=sorted(seen))
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()
        assert request(opener, "/admin/rates", "POST", {"tiers": saved_rates}).status == 200


def shop_defaults_points_table(opener, report, address, password, client_address):
    """Shop › Points table (#106): breadcrumb, scope line, the shared Points row
    editor and the two coverage pills, whose counts and lists follow the Rate
    tiers membership rule and count rows; Edit table links to the Vendo's Points
    table tab. A Shop table edit, from the editor or the old textarea form,
    applies to the next redemption on a Vendo using the Shop table and not on
    one with its own; an empty table offers no redemptions. Leaves the Shop
    Rate tiers and Points table as found."""
    found = json.load(request(opener, "/admin/state"))
    milli = lambda m: f"{m // 1000}.{m % 1000:03d}".rstrip("0").rstrip(".")
    saved_rates = "\n".join(f'{t["pesos"]}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]},{milli(t.get("points_milli", 0))}'
                            for t in found["rate_tiers"])
    saved_points = "\n".join(f'{milli(t["points_milli"])}={t["seconds"]},{t["validity_seconds"]},{t["pause_limit"]}'
                             for t in found["points_tiers"])
    fixture, _, client = vlan_fixture(address, password, client_address)
    paired = []
    seen = set()  # pill wordings, row counts and empty-group sentences the scenario has met

    def page():
        response = request(opener, "/admin/points")
        assert response.status == 200, response.status
        return response, response.read().decode()

    pill_wordings = (("shop", "Vendo uses the Shop table", "Vendos use the Shop table"),
                     ("custom", "Vendo has Custom Points", "Vendos have Custom Points"))

    def expected(online):
        """The lists from /admin/state: paired Vendos only, by display name, split by own Points table."""
        lists = {"shop": [], "custom": []}
        vendos = sorted((v for v in json.load(request(opener, "/admin/state"))["vendos"] if v["status"] == "paired"),
                        key=lambda v: v["name"] or f'Vendo {v["id"]}')
        for v in vendos:
            name = v["name"] or f'Vendo {v["id"]}'
            status = "Deactivated" if not v["active"] else online.get(v["id"], "Offline")
            where = f'{v["location"]} · ' if v["location"] else ""
            if v["points_tiers"] is None:
                lists["shop"].append((v["id"], name, where + status, "Open", f'/admin/vendos?vendo={v["id"]}'))
            else:
                count = len(v["points_tiers"])
                lists["custom"].append((v["id"], name, f'{where}{status} · {count} {"row" if count == 1 else "rows"}',
                                        "Edit table", f'/admin/vendos?vendo={v["id"]}&tab=points'))
                seen.add(f'{count} {"row" if count == 1 else "rows"}')
        return lists

    def check(live):
        """The page's pills and lists match /admin/state; empty groups read as a sentence.
        Each simulated Vendo in live heartbeats first, so the page lists it Online."""
        for vendo in live:
            assert vendo.heartbeat()[0] == 200
        body = page()[1]
        want = expected({vendo.id: "Online" for vendo in live})
        assert shop_pills(body, pill_wordings, seen) == {name: len(items) for name, items in want.items()}, (shop_pills(body, pill_wordings, seen), want)
        for name, empty in (("shop", "No Vendo uses the Shop table."), ("custom", "No Vendo has its own table.")):
            got = shop_rows(body, name)
            assert got == want[name], (name, got, want[name])
            if not want[name]:
                assert empty in shop_group(body, name), shop_group(body, name)
                seen.add(empty)
        assert "A change here applies to each from its next redemption." in shop_group(body, "shop")
        assert "Shop changes do not affect these." in shop_group(body, "custom")
        return body

    def stored():
        return json.load(request(opener, "/admin/state"))["points_tiers"]

    # Page text: breadcrumb, scope line, the shared Points row editor in place of the textarea.
    response, body = page()
    crumbs = re.search(r'<nav id="crumbs"[^>]*>(.*?)</nav>', body, re.S).group(1)
    assert re.search(r"<li[^>]*>Shop</li>.*<li aria-current=\"page\">Points table</li>", crumbs, re.S), crumbs
    assert "<title>Points table" in body, body
    assert "Default Points table for this Shop. Every Vendo uses it unless it has its own." in body, body
    assert re.search(r'<form\b[^>]*action="/admin/points"[^>]*data-tiers="points"', body), body
    assert "Save Shop Points table" in body and "Add row" in body and "<textarea" not in body, body
    assert 'name="id"' not in re.search(r'<form\b[^>]*data-tiers="points".*?</form>', body, re.S).group(0)
    for tier in found["points_tiers"]:
        assert f'data-cost="{milli(tier["points_milli"])}" data-seconds="{tier["seconds"]}"' in body, (tier, body)
    policy = {directive.split()[0]: directive.split()[1:]
              for directive in (response.headers["Content-Security-Policy"] or "").split(";") if directive.strip()}
    assert policy.get("script-src") == ["'self'"] and policy.get("style-src") == ["'self'"], policy
    assert not re.search(r"<style|\sstyle=|\son[a-z]+=|<script(?![^>]*\ssrc=)", body), "inline style or script"
    report.snapshot("shop-points-table-page", address, password, opener)

    shop_points = [{"points_milli": 1000, "seconds": 60, "validity_seconds": 3600, "pause_limit": 1}]
    own_points = [{"points_milli": 2000, "seconds": 300, "validity_seconds": 7200, "pause_limit": 3},
                  {"points_milli": 3000, "seconds": 600, "validity_seconds": 7200, "pause_limit": 3}]
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        # Shop: ₱1 for a minute earning 1 Point, and 1 Point redeems a minute.
        assert request(opener, "/admin/rates", "POST", {"tiers": "1=60,3600,1,1"}).status == 200
        assert request(opener, "/admin/points", "POST", {"tiers": "1=60,3600,1"}).status == 200

        def portal(vlan):
            return portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))

        def buy(vlan, vendo, pesos):
            """The VLAN's Device opens a Coin Window on vendo and pays: its credited seconds."""
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {"vendo_id": vendo.id}))
            window_id = opened["coin_window"]["id"]
            assert vendo.heartbeat()[0] == 200 and vendo.window_id == window_id
            vendo.insert(pesos)
            assert vendo.heartbeat()[0] == 200
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/done", {"window_id": window_id})
            assert vendo.heartbeat()[0] == 200
            assert vendo.heartbeat()[0] == 200
            return coin_window_wait(opener, window_id, lambda w: w["status"] == "settled")[1]["credited_seconds"]

        def redeem(vlan, tier):
            form = {"points_milli": tier["points_milli"], "seconds": tier["seconds"]}
            before = portal(vlan)["time_credit"]
            after = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/points/redeem", form))["time_credit"]
            assert after["points_milli"] == before["points_milli"] - tier["points_milli"], (before, after)
            assert after["bought"] == before["bought"] + tier["seconds"], (before, after)

        def refused(vlan, tier):
            """A row outside the Device's Points table is refused and changes nothing."""
            form = {"points_milli": tier["points_milli"], "seconds": tier["seconds"]}
            before = portal(vlan)["time_credit"]
            try:
                pause_device_fetch(client_address, password, ips[vlan], "/portal/api/points/redeem", form)
            except urllib.error.HTTPError as error:
                assert "400" in json.load(error).get("detail", "")
                assert portal(vlan)["time_credit"]["points_milli"] == before["points_milli"]
                return
            raise AssertionError(f"VLAN {vlan} redeemed a row outside its Points table")

        # Before pairing: an empty Custom group still shows its pill and a sentence.
        check([])
        boards = [pair_board(opener, client_address, password, fixture.board(vlan, n)) for n, vlan in ((1, 10), (2, 20))]
        paired.extend(boards)
        custom, follower = (SimulatedVendo(BASE_URL, b["id"], bytes.fromhex(b["secret"]), timeout=HTTP_TIMEOUT) for b in boards)
        for vendo in (custom, follower):
            assert vendo.heartbeat()[0] == 200
        live = [custom, follower]
        body = check(live)
        report.snapshot("shop-points-table-pills-follow-paired-vendos", address, password, opener, pills=shop_pills(body, pill_wordings, seen))

        # Customize copies the Shop table (1 row); the Vendo's own table has 2 rows.
        assert request(opener, "/admin/vendos/points", "POST", {"id": custom.id, "action": "customize"}).status == 200
        check(live)
        assert request(opener, "/admin/vendos/points", "POST", {"id": custom.id, "action": "save",
                                                                "tiers": "2=300,7200,3\n3=600,7200,3"}).status == 200
        body = check(live)
        assert any(r[0] == custom.id and r[4] == f"/admin/vendos?vendo={custom.id}&tab=points" for r in shop_rows(body, "custom")), shop_rows(body, "custom")
        # Deactivated stays listed, with its status; a Custom Rate tier set does not move a Vendo here.
        assert request(opener, "/admin/vendos/rates", "POST", {"id": follower.id, "action": "customize"}).status == 200
        assert request(opener, "/admin/vendos/active", "POST", {"id": follower.id, "active": "0"}).status == 200
        body = check(live)
        assert any(r[0] == follower.id and r[2].endswith("Deactivated") for r in shop_rows(body, "shop")), shop_rows(body, "shop")
        assert request(opener, "/admin/vendos/active", "POST", {"id": follower.id, "active": "1"}).status == 200
        assert request(opener, "/admin/vendos/rates", "POST", {"id": follower.id, "action": "site"}).status == 200
        report.snapshot("shop-points-table-custom-and-deactivated", address, password, opener, pills=shop_pills(body, pill_wordings, seen))

        # Each Device earns Points on its own VLAN's Vendo.
        assert buy(10, custom, 10) == 600 and buy(20, follower, 10) == 600
        assert portal(10)["points"] == own_points and portal(20)["points"] == shop_points, (portal(10), portal(20))

        # A Shop table edit from the editor: the follower's next redemption uses it, the Custom Vendo's does not.
        response = request(opener, "/admin/points", "POST", [("action", "save"), ("tier_cost", "1"), ("tier_minutes", "2"),
                                                             ("tier_validity", "1"), ("tier_unit", "hours"), ("tier_pause", "1")])
        assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/points", response.status
        edited = [{"points_milli": 1000, "seconds": 120, "validity_seconds": 3600, "pause_limit": 1}]
        assert stored() == edited, stored()
        assert 'data-cost="1" data-seconds="120" data-validity="3600" data-pause="1"' in page()[1]
        assert portal(20)["points"] == edited and portal(10)["points"] == own_points, (portal(20), portal(10))
        refused(20, shop_points[0])
        redeem(20, edited[0])
        refused(10, edited[0])
        redeem(10, own_points[0])
        report.snapshot("shop-points-table-edit-applies-to-next-redemption", address, password, opener,
                        follower_redeemed=edited[0], custom_redeemed=own_points[0])

        # The old textarea form still saves the Shop table.
        response = request(opener, "/admin/points", "POST", {"tiers": "0.5=90,3600,1"})
        assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin/points", response.status
        textarea = [{"points_milli": 500, "seconds": 90, "validity_seconds": 3600, "pause_limit": 1}]
        assert stored() == textarea and portal(20)["points"] == textarea, stored()
        redeem(20, textarea[0])
        report.snapshot("shop-points-table-textarea-save", address, password, opener)

        # An empty Shop table: the follower cannot redeem, the Custom Vendo still can.
        assert request(opener, "/admin/points", "POST", {"tiers": ""}).status == 200
        assert stored() == [] and portal(20)["points"] == [] and portal(10)["points"] == own_points
        assert "data-tier-seed" not in re.search(r'<form\b[^>]*data-tiers="points".*?</form>', page()[1], re.S).group(0)
        refused(20, textarea[0])
        redeem(10, own_points[0])
        report.snapshot("shop-points-table-empty-offers-no-redemptions", address, password, opener)

        # Unpaired Vendos leave both lists.
        assert request(opener, "/admin/vendos/unpair", "POST", {"id": follower.id}).status == 204
        paired.remove(boards[1])
        body = check([custom])
        assert all(r[0] != follower.id for r in shop_rows(body, "shop") + shop_rows(body, "custom"))
        for wording in ("Vendo uses the Shop table", "Vendos use the Shop table", "Vendo has Custom Points",
                        "1 row", "2 rows", "No Vendo has its own table."):
            assert wording in seen, (wording, seen)
        report.snapshot("shop-points-table-exclude-unpaired", address, password, opener, wordings=sorted(seen))
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()
        assert request(opener, "/admin/rates", "POST", {"tiers": saved_rates}).status == 200
        assert request(opener, "/admin/points", "POST", {"tiers": saved_points}).status == 200


def vlan_fixture(address, password, client_address):
    router = lambda path, method="GET", body=None: router_rest(address, "admin", password, path, method, body)
    client = lambda path, method="GET", body=None: router_rest(client_address, "admin", password, path, method, body, scheme="http")
    return _vlan_module.VlanLab(router, client), router, client


def route_vlan_devices(fixture, client):
    """Route each set-up VLAN Device out its own VLAN on the client router: {vlan: Device IP}.
    Undo with unroute_vlan_devices."""
    ips = {item["vlan"]: item["host_rows"][0]["address"] for item in fixture.hosts()}
    for vlan, ip in ips.items():
        name = f"opnw-vlan{vlan}"
        if not client(f"/routing/table?name={name}"):
            client("/routing/table", "PUT", {"name": name, "fib": "yes"})
        client("/ip/route", "PUT", {"dst-address": "0.0.0.0/0", "gateway": f"10.5.{vlan}.1%{name}", "routing-table": name, "comment": name})
        client("/routing/rule", "PUT", {"src-address": ip + "/32", "action": "lookup-only-in-table", "table": name, "comment": name})
    return ips


def unroute_vlan_devices(client):
    """Remove the routes and rules route_vlan_devices added."""
    for vlan in (10, 20):
        name = f"opnw-vlan{vlan}"
        for path in ("/routing/rule", "/ip/route"):
            for row in client(f"{path}?comment={name}"):
                client(f"{path}/{row['.id']}", "DELETE")


def close_and_settle(opener, client_address, password, ip, board, window, cookie=""):
    vendo = SimulatedVendo(BASE_URL, board["id"], bytes.fromhex(board["secret"]), timeout=HTTP_TIMEOUT)
    assert vendo.heartbeat()[0] == 200 and vendo.window_id == window["id"]
    pause_device_fetch(client_address, password, ip, "/portal/api/coins/done", {"window_id": window["id"]}, cookie)
    assert vendo.heartbeat()[0] == 200
    assert vendo.heartbeat()[0] == 200
    coin_window_wait(opener, window["id"], lambda w: w["status"] == "settled")


def vendo_auto_select(opener, address, password, client_address, report):
    fixture, router, client = vlan_fixture(address, password, client_address)
    paired = []
    try:
        fixture.setup()
        ips = route_vlan_devices(fixture, client)
        board = pair_board(opener, client_address, password, fixture.board(10, 1))
        paired.append(board)
        for vlan in (10, 20):
            state = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))
            assert vendo_ids(state) == [board["id"]], state
            opened = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {}))
            assert opened["coin_window"]["vendo_id"] == board["id"]
            window = next(w for w in json.load(request(opener, "/admin/state"))["coin_windows"] if w["status"] == "open")
            assert window["vendo_id"] == board["id"]
            close_and_settle(opener, client_address, password, ips[vlan], board, window)
        report.snapshot("single-vendo-auto-select-from-either-vlan", address, password, opener)
        second = pair_board(opener, client_address, password, fixture.board(20, 2))
        paired.append(second)
        state = json.load(request(opener, "/admin/state"))
        for board, vlan in ((paired[0], 10), (second, 20)):
            stored = next(v for v in state["vendos"] if v["id"] == board["id"])
            assert stored.get("hotspot_server") == f"opnw-vlan{vlan}", stored
            portal = portal_state(pause_device_fetch(client_address, password, ips[vlan], "/portal/api/state"))
            assert vendo_ids(portal) == [board["id"]], portal
            pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", {})
            window = next(w for w in json.load(request(opener, "/admin/state"))["coin_windows"] if w["status"] == "open")
            assert window["vendo_id"] == board["id"]
            close_and_settle(opener, client_address, password, ips[vlan], board, window)
        report.snapshot("one-vendo-per-vlan-learned-and-auto-selected", address, password, opener)
        third = pair_board(opener, client_address, password, fixture.board(10, 3))
        paired.append(third)
        state = portal_state(pause_device_fetch(client_address, password, ips[10], "/portal/api/state"))
        assert vendo_ids(state) == [paired[0]["id"], third["id"]], state
        def windows():
            return json.load(request(opener, "/admin/state"))["coin_windows"]
        def rejected(vlan, form):
            before = windows()
            try:
                pause_device_fetch(client_address, password, ips[vlan], "/portal/api/coins/open", form)
            except urllib.error.HTTPError as error:
                assert "400" in json.load(error).get("detail", "")
            else:
                raise AssertionError("invalid Vendo selection was accepted")
            assert windows() == before, "rejected selection reserved a Vendo"
        rejected(10, {"vendo_id": second["id"]})
        rejected(10, {})
        result = pause_device_fetch(client_address, password, ips[10], "/portal/api/coins/open", {"vendo_id": third["id"]})
        cookie = re.search(r"(?:^|,)Set-Cookie: (openpiso_time_credit=[^;,\r\n]+)", result.get("http-headers", ""), re.I).group(1)
        window = next(w for w in windows() if w["status"] == "open")
        assert window["vendo_id"] == third["id"]
        report.snapshot("picker-is-limited-to-network-and-forged-id-reserves-nothing", address, password, opener)
        # The customer's browser moves to lan2/VLAN 20 with its ownership cookie.
        interface = client("/interface/vlan?name=opnw-vlan10")[0]
        client(f"/interface/vlan/{interface['.id']}", "PATCH", {"disabled": "yes"})
        # The cookie arrives on a new Device: state only flags the Rebind; POST connect does it.
        unbound = portal_state(pause_device_fetch(client_address, password, ips[20], "/portal/api/state", cookie=cookie))
        assert unbound["rebind"] and unbound["coin_window"] is None, unbound
        rebound = portal_state(pause_device_fetch(client_address, password, ips[20], "/portal/api/connect", {}, cookie))
        assert not rebound["rebind"], rebound
        moved = portal_state(pause_device_fetch(client_address, password, ips[20], "/portal/api/state", cookie=cookie))
        assert moved["coin_window"]["vendo_id"] == third["id"] and moved["coin_window"]["status"] == "open", moved
        assert next(w for w in windows() if w["id"] == window["id"])["vendo_id"] == third["id"]
        close_and_settle(opener, client_address, password, ips[20], third, window, cookie)
        client(f"/interface/vlan/{interface['.id']}", "PATCH", {"disabled": "no"})
        report.snapshot("coin-window-keeps-reserved-vendo-after-vlan-move", address, password, opener)
        page = request(opener, "/admin/vendos").read().decode()
        assert 'select name="hotspot_server"' in page
        assert 'value="opnw-vlan10"' in page and 'value="opnw-vlan20"' in page
        assert request(opener, "/admin/vendos/edit", "POST", {"id": second["id"], "name": "", "location": "", "hotspot_server": "missing-server"}).status == 400
        assert request(opener, "/admin/vendos/edit", "POST", {"id": second["id"], "name": "", "location": "", "hotspot_server": "opnw-vlan10"}).status == 200
        state = json.load(request(opener, "/admin/state"))
        assert next(v for v in state["vendos"] if v["id"] == second["id"])["hotspot_server"] == "opnw-vlan10"
        state = portal_state(pause_device_fetch(client_address, password, ips[20], "/portal/api/state"))
        assert state["vendos"] == [], state
        rejected(20, {})
        rejected(20, {"vendo_id": second["id"]})
        report.snapshot("admin-overrides-network-and-no-match-reserves-nothing", address, password, opener)
    finally:
        report.capture_lab_failure()
        for board in paired:
            assert request(opener, "/admin/vendos/unpair", "POST", {"id": board["id"]}).status == 204
        unroute_vlan_devices(client)
        fixture.teardown()


def vlan_host_evidence(address, password, client_address, report, write_hosts):
    fixture, _, _ = vlan_fixture(address, password, client_address)
    try:
        fixture.setup()
        evidence = fixture.hosts()
        if write_hosts:
            destination = ROOT / "docs/research/results/2026-10-29-chr-vlan-autoselect/hosts.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(evidence, indent=2) + "\n")
        report.data["vlan_hosts"] = evidence
        for item in evidence:
            assert len(item["host_rows"]) == 1, item
            assert item["host_rows"][0].get("server") == f"opnw-vlan{item['vlan']}", item
    finally:
        report.capture_lab_failure()
        fixture.teardown()


def hotspot_tree(address, password):
    """Everything connecting must leave alone: HotSpot servers, server profiles, login pages and walled garden."""
    def rows(path):
        return router_rest(address, "admin", password, path)
    # A full /file listing includes binary contents that are not UTF-8, so list names only.
    pages = sorted(row["name"] for row in rows("/file?.proplist=name") if row["name"].endswith("login.html"))
    return {
        "servers": rows("/ip/hotspot"),
        "server_profiles": rows("/ip/hotspot/profile"),
        "walled_garden": rows("/ip/hotspot/walled-garden/ip"),
        "login_pages": {name: rows("/file?name=" + urllib.parse.quote(name, safe=""))[0] for name in pages},
    }


def router_backups(address, password):
    return sorted(row["name"] for row in router_rest(address, "admin", password, "/file?type=backup&.proplist=name")
                  if row["name"].startswith("openpiso-"))


def router_setup_finished(opener):
    """The Dashboard's Router setup notice: (finished, page has the notice linking to Router setup)."""
    html = request(opener, "/admin/dashboard").read().decode()
    notice = bool(re.search(r'<a class="setup-banner" href="/admin">Router setup is not finished', html))
    return dashboard(opener)["router_setup_finished"], notice


def checklist(page):
    """The Router setup checklist on a page: {(server, check): {"met", "reason", "fix"}}, server "" for the Router.
    fix is the label of the item's one-click fix, or None; manual is its manual steps, or None."""
    items = {}
    for match in re.finditer(r'<li class="check" data-check="([^"]+)" data-server="([^"]*)" data-met="(true|false)">(.*?)</li>', page, re.S):
        name, server, met, body = match.groups()
        reason = re.search(r'<span class="check-reason">(.*?)</span>', body, re.S)
        fix = re.search(r'<form method="post" action="/admin/router/fix">.*?<button[^>]*>(.*?)</button>', body, re.S)
        manual = re.search(r'<p class="check-manual">(.*?)</p>', body, re.S)
        items[(server, name)] = {"met": met == "true", "reason": reason and html.unescape(reason.group(1)),
                                 "fix": fix and html.unescape(fix.group(1)), "manual": manual and html.unescape(manual.group(1))}
    return items


def unmet(items):
    """The (server, check) keys of the items that are not met."""
    return sorted(key for key, item in items.items() if not item["met"])


def use_existing(opener, report, address, password, existing, tree_before, other_before, users_before):
    """Router setup's Use existing: Adopt hs1 and leave the second server unticked."""
    server, other = _hotspot_module.SERVER, _hotspot_module.OTHER
    user_profiles_before = router_rest(address, "admin", password, "/ip/hotspot/user/profile")
    page = request(opener, "/admin").read().decode()
    for name in (server, other):
        assert f'<input type="checkbox" name="server" value="{name}" checked>' in page, page
    # Before anything is Adopted the checklist reads the Router alone: what someone else's HotSpot lacks.
    router_items = checklist(page)
    assert sorted(router_items) == [("", "base-profiles"), ("", "routeros-v7"), ("", "walled-garden")], router_items
    assert unmet(router_items) == [("", "base-profiles"), ("", "walled-garden")], router_items
    assert all(router_items[key]["reason"] and router_items[key]["fix"] for key in unmet(router_items)), router_items
    response = request(opener, "/admin/router/use-existing?" + urllib.parse.urlencode({"server": server}))
    review = response.read().decode()
    assert response.status == 200, review
    assert "login page" in review and "login method" in review and server in review and other not in review, review
    assert 'action="/admin/router/adopt"' in review
    # The review reports the ticked server's items before Adopting fixes any; it offers no fix itself.
    server_items = checklist(review)
    assert sorted(server_items) == [(server, "device-seen"), (server, "login-method"), (server, "login-page"),
                                    (server, "portal-reachable"), (server, "server-enabled")], server_items
    assert unmet(server_items) == [(server, "device-seen"), (server, "login-method"), (server, "login-page"),
                                   (server, "portal-reachable")], server_items
    assert "http-pap" in server_items[(server, "login-method")]["reason"], server_items
    assert all(item["fix"] is None for item in server_items.values()), server_items
    for form, status in (({}, 400), ({"server": "opnw-missing"}, 400)):
        assert request(opener, "/admin/router/adopt", "POST", form).status == status, form
    assert hotspot_tree(address, password) == tree_before, "the checklist, reviewing or a refused Adopt changed the Router"
    assert router_rest(address, "admin", password, "/ip/hotspot/user/profile") == user_profiles_before, "the checklist wrote a profile"
    assert json.load(request(opener, "/admin/state"))["router"]["adopted_hotspot_servers"] == []
    report.snapshot("use-existing-lists-servers-ticked-and-names-both-changes", address, password, opener,
                    review_html=review)

    response = request(opener, "/admin/router/adopt", "POST", {"server": server})
    page = response.read().decode()
    assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin", page
    assert f'<input type="checkbox" name="server" value="{other}" checked>' in page, page
    assert f'<input type="checkbox" name="server" value="{server}"' not in page, page
    state = json.load(request(opener, "/admin/state"))
    assert state["router"]["adopted_hotspot_servers"] == [server]
    assert state["router"]["status"] == "ready"
    profile = router_rest(address, "admin", password, f"/ip/hotspot/profile?name={_hotspot_module.SERVER_PROFILE}")[0]
    assert (profile["login-by"], profile["html-directory"]) == ("http-pap", _hotspot_module.CONTROLLER_LOGIN_DIRECTORY), profile
    files = router_rest(address, "admin", password, "/file?name=openpiso%2Flogin.html")
    assert len(files) == 1
    assert f"{PORTAL_URL}/portal" in files[0]["contents"]
    assert "$(mac-esc)" in files[0]["contents"] and "$(ip)" in files[0]["contents"]
    # The second server, its server profile and the default login page read back unchanged.
    assert existing.other() == other_before
    assert other_before["login_page"][0]["contents"] == _hotspot_module.LOGIN_HTML
    assert existing.users() == users_before

    profiles = router_rest(address, ROUTER_ADMIN, ROUTER_ADMIN_PASSWORD, "/ip/hotspot/user/profile")
    by_name = {profile["name"]: profile for profile in profiles}
    assert by_name["openpiso-no-idle"]["idle-timeout"] == "none"
    assert by_name["openpiso-idle"]["idle-timeout"] == "2m"
    for name in ("openpiso-no-idle", "openpiso-idle"):
        assert by_name[name]["shared-users"] == "1"
        assert by_name[name]["keepalive-timeout"] == "none"
        assert by_name[name]["add-mac-cookie"] == "false"
    walled = router_rest(address, ROUTER_ADMIN, ROUTER_ADMIN_PASSWORD, "/ip/hotspot/walled-garden/ip")
    controller_rules = [entry for entry in walled if entry.get("comment") == "openpiso-controller"]
    assert len(controller_rules) == 1
    assert controller_rules[0]["dst-address"] == urllib.parse.urlsplit(PORTAL_URL).hostname
    assert controller_rules[0]["dst-port"] == "18080"
    assert router_setup_finished(opener) == (True, False)
    # After Adopting items 8 and 9 are open: Adopting adds no NAT rule, and no Device has been sent to the Portal yet.
    items = checklist(page)
    assert unmet(items) == [(server, "device-seen"), (server, "portal-reachable")], items
    assert items[(server, "device-seen")]["fix"] is None and items[(server, "device-seen")]["reason"], items
    assert not any(key[0] == other for key in items), items
    report.snapshot("use-existing-adopts-only-the-ticked-server", address, password, opener,
                    adopted=[server], unticked=other, server_profile=profile,
                    default_login_page_unchanged=True)


def without_hotspot_servers(address, password, check):
    """Run check() while the lab Router has no HotSpot server, then put hs1 back as it was."""
    row = router_rest(address, "admin", password, f"/ip/hotspot?name={_hotspot_module.SERVER}")[0]
    kept = {key: row[key] for key in ("name", "interface", "profile", "address-pool", "idle-timeout",
                                      "addresses-per-mac", "keepalive-timeout", "login-timeout", "disabled") if key in row}
    router_rest(address, "admin", password, f"/ip/hotspot/{row['.id']}", "DELETE")
    try:
        check()
    finally:
        router_rest(address, "admin", password, "/ip/hotspot", "PUT", kept)


def drift(opener, report, address, password):
    """Break checklist items by hand on the Router; the checklist reports each, and its fix clears it alone."""
    server, profile_name = _hotspot_module.SERVER, _hotspot_module.SERVER_PROFILE
    for form in ({"check": "login-method", "server": "opnw-missing"}, {"check": "no-such-check", "server": ""}):
        assert request(opener, "/admin/router/fix", "POST", form).status == 400, form
    for name, path, query, broken in (
        ("login-method", "/ip/hotspot/profile", f"name={profile_name}", {"login-by": "cookie,http-pap"}),
        ("server-enabled", "/ip/hotspot", f"name={server}", {"disabled": "yes"}),
    ):
        tree_before = hotspot_tree(address, password)
        target = router_rest(address, "admin", password, f"{path}?{query}")[0]
        router_rest(address, "admin", password, f"{path}/{target['.id']}", "PATCH", broken)
        items = checklist(request(opener, "/admin").read().decode())
        assert unmet(items) == [(server, name)], items
        assert items[(server, name)]["reason"] and items[(server, name)]["fix"], items
        response = request(opener, "/admin/router/fix", "POST", {"check": name, "server": server})
        page = response.read().decode()
        assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin", page
        assert unmet(checklist(page)) == [], page
        assert hotspot_tree(address, password) == tree_before, f"the {name} fix changed more than its item"
        report.snapshot(f"drift-{name}-reported-and-cleared-by-its-fix", address, password, opener,
                        broken=broken, reason=items[(server, name)]["reason"], fix=items[(server, name)]["fix"])


def portal_reachability(opener, report, address, password):
    """Item 8: a masquerade that source-NATs hs1's Devices on the way to the Portal is reported, its fix adds one
    Controller rule above it and leaves the other NAT rules as they were, and a lost return route is manual."""
    server = _hotspot_module.SERVER
    def rest(path, method="GET", body=None):
        return router_rest(address, "admin", password, path, method, body)
    def owned(row):
        return row.get("comment", "").startswith("openpiso-controller")
    def others():  # every NAT rule the Controller does not own, without its traffic counters
        return [{key: value for key, value in row.items() if key not in ("bytes", "packets")}
                for row in rest("/ip/firewall/nat") if not owned(row)]
    # The harness's own exemption would hide the masquerade; the Controller's fix takes its place.
    for row in rest("/ip/firewall/nat?comment=openpiso-e2e-routing") + rest("/ip/firewall/nat?comment=openpiso-e2e-masquerade"):
        rest(f"/ip/firewall/nat/{row['.id']}", "DELETE")
    # Like the masquerade the HotSpot setup wizard adds for the server's network.
    rest("/ip/firewall/nat", "PUT", {"chain": "srcnat", "action": "masquerade", "src-address": "10.5.50.0/24",
                                     "comment": "openpiso-e2e-masquerade"})
    nat_before = others()
    items = checklist(request(opener, "/admin").read().decode())
    assert unmet(items) == [(server, "device-seen"), (server, "portal-reachable")], items
    broken = items[(server, "portal-reachable")]
    assert broken["reason"] and broken["fix"] and broken["manual"] is None, broken
    response = request(opener, "/admin/router/fix", "POST", {"check": "portal-reachable", "server": server})
    page = response.read().decode()
    assert response.status == 200 and urllib.parse.urlsplit(response.url).path == "/admin", page
    assert unmet(checklist(page)) == [(server, "device-seen")], page
    assert others() == nat_before, "the fix changed a NAT rule it does not own"
    rules = rest("/ip/firewall/nat")
    exemptions = [row for row in rules if owned(row)]
    assert len(exemptions) == 1, exemptions
    exemption = exemptions[0]
    assert (exemption["chain"], exemption["action"], exemption["src-address"], exemption["dst-address"]) == (
        "srcnat", "accept", "10.5.50.0/24", urllib.parse.urlsplit(PORTAL_URL).hostname), exemption
    srcnat = [row for row in rules if row["chain"] == "srcnat" and row.get("dynamic") != "true"]
    assert srcnat[0][".id"] == exemption[".id"], srcnat
    report.snapshot("masquerade-reported-and-exempted-by-one-controller-rule", address, password, opener,
                    reason=broken["reason"], fix=broken["fix"], exemption=exemption)

    # Without the routing NIC the Controller's replies to 10.5.50.1 have no way back: manual, no fix.
    nic = rest("/interface/ethernet?mac-address=52:54:00:07:07:07")[0]
    rest(f"/interface/ethernet/{nic['.id']}", "PATCH", {"disabled": "yes"})
    try:
        items = checklist(request(opener, "/admin").read().decode())
        unroutable = items[(server, "portal-reachable")]
        assert unmet(items) == [(server, "device-seen"), (server, "portal-reachable")], items
        assert unroutable["reason"] and unroutable["manual"] and unroutable["fix"] is None, unroutable
        assert request(opener, "/admin/router/fix", "POST", {"check": "portal-reachable", "server": server}).status == 400
    finally:
        rest(f"/interface/ethernet/{nic['.id']}", "PATCH", {"disabled": "no"})
    assert others() == nat_before
    deadline = time.monotonic() + 30
    while unmet(items := checklist(request(opener, "/admin").read().decode())) != [(server, "device-seen")]:
        assert time.monotonic() < deadline, items
        time.sleep(2)
    report.snapshot("missing-return-route-reported-with-manual-steps", address, password, opener,
                    reason=unroutable["reason"], manual=unroutable["manual"])


def onboarding(controller, args, data_dir, opener, report, address, password, client_address, existing, other_before):
    """Use existing on someone else's HotSpot, after the shared onboarding Adopted hs1 (see use_existing)."""
    assert existing.other() == other_before, "Controller boot changed the server that was not ticked"
    report.snapshot("boot-leaves-the-unticked-server-unchanged", address, password, opener, unticked=existing.other())
    existing.teardown_other()

    server = _hotspot_module.SERVER
    portal_reachability(opener, report, address, password)
    redirect_page, _ = device_redirect(client_address, password)
    assert f"{PORTAL_URL}/portal" in redirect_page, redirect_page
    device_ip = router_rest(client_address, "admin", password, "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
    # The Portal page reads its state on load, as a Device's browser does.
    portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/state"))
    items = checklist(request(opener, "/admin").read().decode())
    assert unmet(items) == [], items
    report.snapshot("device-sent-to-the-portal-meets-the-last-item", address, password, opener,
                    checklist={f"{key[0]}/{key[1]}": item["met"] for key, item in items.items()})
    response = generate_vouchers(opener, {"seconds": "600", "count": "2"})
    assert response.status == 200
    codes = voucher_codes(opener, response)
    state = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/redeem", {"code": codes[0]}))
    assert_message(state, "success", "online")
    target, internet = device_internet(client_address, password, device_ip)
    assert PORTAL_URL.removeprefix("http://") + "/portal" not in internet
    report.snapshot("adopted-server-sends-a-device-online-with-a-voucher", address, password, opener,
                    redirect_html=redirect_page, internet_target=target)

    # A ledger from before Router setup: a saved Router connection and no record of Adopted servers.
    # The Router is as that Controller left it, the server profile on the default directory.
    controller.stop()
    with sqlite3.connect(Path(data_dir) / "openpiso.db") as db:
        db.execute("DROP TABLE adopted_hotspot_servers")
    db.close()
    profile = router_rest(address, "admin", password, f"/ip/hotspot/profile?name={_hotspot_module.SERVER_PROFILE}")[0]
    router_rest(address, "admin", password, f"/ip/hotspot/profile/{profile['.id']}", "PATCH", {"html-directory": "hotspot"})
    for active in router_rest(address, "admin", password, "/ip/hotspot/active"):
        if active.get("user", "").startswith("op-"):
            router_rest(address, "admin", password, f"/ip/hotspot/active/{active['.id']}", "DELETE")
    controller = Controller(args.binary, data_dir, initialize=False)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
    state = json.load(request(opener, "/admin/state"))
    assert state["router"]["status"] == "ready", state["router"]
    assert state["router"]["adopted_hotspot_servers"] == [_hotspot_module.SERVER]
    assert router_setup_finished(opener) == (True, False)
    page = request(opener, "/admin").read().decode()
    assert 'name="server"' not in page, page
    profile = router_rest(address, "admin", password, f"/ip/hotspot/profile?name={_hotspot_module.SERVER_PROFILE}")[0]
    assert profile["html-directory"] == _hotspot_module.CONTROLLER_LOGIN_DIRECTORY, profile
    connected = portal_state(pause_device_fetch(client_address, password, device_ip, "/portal/api/connect", {}))
    assert_message(connected, "success", "online")
    target, internet = device_internet(client_address, password, device_ip)
    assert PORTAL_URL.removeprefix("http://") + "/portal" not in internet
    report.snapshot("ledger-from-before-router-setup-starts-adopted-and-brings-a-device-online", address, password, opener,
                    adopted=state["router"]["adopted_hotspot_servers"], internet_target=target)

    drift(opener, report, address, password)

    # Last: a re-created HotSpot server takes a while before a Device fetch through it works.
    def no_use_existing():
        page = request(opener, "/admin").read().decode()
        assert 'action="/admin/router/use-existing"' not in page, page
        assert "no HotSpot server" in page, page
    without_hotspot_servers(address, password, no_use_existing)
    report.snapshot("use-existing-hidden-without-a-hotspot-server", address, password, opener)
    return controller, opener


def scenario(args, report: Report, lab: dict[str, str]) -> None:
    address = router_address()
    configure_router_tls(address, lab["CHR_PASSWORD"])
    client_address = start_client(lab["CHR_PASSWORD"])
    client = lambda path, method="GET", body=None: router_rest(client_address, "admin", lab["CHR_PASSWORD"], path, method, body, scheme="http")
    report.data["chr_client_cleanup"] = {}
    _lab_module.cleanup(client, report.data["chr_client_cleanup"])
    cleanup_vendo_router(address, lab["CHR_PASSWORD"])
    interface = client("/interface/ethernet?name=lan1")[0]
    client(f"/interface/ethernet/{interface['.id']}", "PATCH", {"disabled": "no"})
    renew_device_lease(client_address, lab["CHR_PASSWORD"], verify_portal=False)
    report.data["chr_client_cleanup"]["device_lease"] = client("/ip/dhcp-client?interface=lan1")
    vlan_host_evidence(address, lab["CHR_PASSWORD"], client_address, report, args.write_vlan_hosts)
    resource = router_rest(address, ROUTER_ADMIN, ROUTER_ADMIN_PASSWORD, "/system/resource")
    assert resource["version"].startswith("7.")
    original_hotspot_names = {entry["name"] for entry in router_rest(address, "admin", lab["CHR_PASSWORD"], "/ip/hotspot/user")}
    existing = _hotspot_module.ExistingHotspotLab(
        lambda path, method="GET", body=None: router_rest(address, "admin", lab["CHR_PASSWORD"], path, method, body))
    try:
        with tempfile.TemporaryDirectory(prefix="openpiso-e2e-") as data_dir:
            def restart_browser_controller():
                nonlocal controller
                controller.stop()
                controller = Controller(args.binary, data_dir, initialize=False)
                return {}

            controller = Controller(args.binary, data_dir, initialize=True)
            try:
                with urllib.request.urlopen(f"{BASE_URL}/portal", timeout=2) as response:
                    body = response.read().decode()
                assert response.status == 200
                assert "OpenPiso WiFi" in body and "/portal/core.js" in body
                # The Portal CSP keeps loads and connections on the Controller and refuses
                # inline styles and scripts; the Default Portal needs neither.
                policy = {directive.split()[0]: directive.split()[1:]
                          for directive in (response.headers["Content-Security-Policy"] or "").split(";") if directive.strip()}
                assert policy.get("default-src") == ["'self'"], policy
                assert all(policy.get(name, ["'self'"]) == ["'self'"]
                           for name in ("script-src", "style-src", "img-src", "connect-src", "font-src")), policy
                assert policy.get("frame-ancestors") == ["'none'"] and policy.get("base-uri") == ["'none'"], policy
                assert not re.search(r"<style|\sstyle=|\son[a-z]+=|<script(?![^>]*\ssrc=)", body), "inline style or script"
                opener = urllib.request.build_opener()
                for path, kind in (("/portal/core.js", "javascript"), ("/portal/portal.css", "text/css")):
                    response = request(opener, path)
                    assert response.status == 200 and kind in response.headers["Content-Type"], path
                # Before onboarding the Portal API still answers in its JSON shape.
                response = request(opener, "/portal/api/state")
                assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
                state = json.load(response)
                assert state["time_credit"] is None and not state["rebind"]
                assert_message(state, "error", "unavailable")

                for _ in range(5):
                    response = request(opener, "/admin/login", "POST", {"password": "wrong"})
                    assert response.status == 401
                response = request(opener, "/admin/login", "POST", {"password": "wrong"})
                assert response.status == 429
            finally:
                report.capture_lab_failure()
                controller.stop()

            controller = Controller(args.binary, data_dir, initialize=False)
            try:
                cookies = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                response = request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD})
                assert response.status == 200
                assert urllib.parse.urlsplit(response.url).path == "/admin/dashboard"
                assert any(cookie.name == "openpiso_admin" and cookie.has_nonstandard_attr("HttpOnly") for cookie in cookies)

                response = request(opener, "/admin/state")
                assert response.status == 200
                state = json.load(response)
                assert state["database"]["journal_mode"] == "wal"
                assert state["database"]["synchronous"] == "full"
                assert not contains_secret_key(state)
                report.snapshot("boot-admin-password-persisted-and-rate-limited", address, lab["CHR_PASSWORD"], opener,
                                login_failures=5, sixth_status=429, journal_mode="wal", synchronous="full")

                response = request(opener, "/admin/routes")
                assert response.status == 200
                routes = json.load(response)
                changing_routes = [route for route in routes if route["state_changing"]]
                assert changing_routes
                assert all(route["method"] == "POST" for route in changing_routes)
                assert all(route["authentication"] != "none" for route in changing_routes)
                anonymous = urllib.request.build_opener()
                for route in changing_routes:
                    if route["authentication"] == "admin-session":
                        assert request(anonymous, route["path"], "POST").status == 401
                    assert request(opener, route["path"]).status in (200, 405)
                    assert request(opener, route["path"], "POST", headers={"Origin": "http://attacker.invalid"}).status == 403
                report.snapshot("authenticated-post-route-policy", address, lab["CHR_PASSWORD"], opener, routes=routes)

                if args.only == "pairing":
                    provisioned = json.load(request(opener, "/admin/vendos/pair", "POST", {"mac": "02:00:00:42:00:01"}))
                    assert not json.load(request(opener, "/admin/state")).get("router", {}).get("configured", False)
                    assert request(opener, "/admin/vendos/unpair", "POST", {"id": provisioned["id"]}).status == 204
                    report.snapshot("provisioned-unpair-needs-no-router", address, lab["CHR_PASSWORD"], opener)

                non_v7_refusal(opener, report, address, lab["CHR_PASSWORD"])

                # Every run connects to someone else's HotSpot (lab/chr/hotspot_lab.py) and Adopts hs1.
                existing.setup()
                tree_before_connect = hotspot_tree(address, lab["CHR_PASSWORD"])
                other_before = existing.other()
                users_before = existing.users()
                assert router_backups(address, lab["CHR_PASSWORD"]) == []
                report.snapshot("router-holds-someone-elses-hotspot", address, lab["CHR_PASSWORD"], opener,
                                hotspot=tree_before_connect, users=users_before)

                response = request(
                    opener,
                    "/admin/router/probe",
                    "POST",
                    {
                        "address": f"https://{address}",
                        "username": ROUTER_ADMIN,
                        "password": ROUTER_ADMIN_PASSWORD,
                    },
                )
                assert response.status == 200
                probe_page = response.read().decode()
                fingerprint = re.search(r"SHA256:(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", probe_page)
                assert fingerprint, probe_page
                assert ROUTER_ADMIN_PASSWORD not in probe_page
                certificates = router_rest(address, "admin", lab["CHR_PASSWORD"], "/certificate?name=openpiso-e2e-server")
                router_fingerprint = certificates[0]["fingerprint"].upper()
                assert fingerprint.group(0).removeprefix("SHA256:").replace(":", "") == router_fingerprint
                report.snapshot("operator-compares-certificate", address, lab["CHR_PASSWORD"], opener,
                                displayed_fingerprint=fingerprint.group(0), router_fingerprint=router_fingerprint)

                # Connecting lands on Router setup and changes no HotSpot configuration.
                response = request(opener, "/admin/router/confirm", "POST")
                setup_page = response.read().decode()
                assert response.status == 200, setup_page
                assert urllib.parse.urlsplit(response.url).path == "/admin", response.url
                assert "Router setup" in setup_page and "RouterOS 7." in setup_page, setup_page
                assert hotspot_tree(address, lab["CHR_PASSWORD"]) == tree_before_connect, "connecting changed the HotSpot"
                assert existing.users() == users_before
                backups = router_backups(address, lab["CHR_PASSWORD"])
                assert len(backups) == 1 and backups[0].endswith(".backup"), backups
                assert router_setup_finished(opener) == (False, True)
                report.snapshot("connecting-changes-no-hotspot-and-saves-a-backup", address, lab["CHR_PASSWORD"], opener,
                                backups=backups, setup_html=setup_page)

                response = request(opener, "/admin/state")
                state = json.load(response)
                assert state["router"] == {
                    "address": f"https://{address}",
                    "adopted_hotspot_servers": [],
                    "configured": True,
                    "fingerprint": fingerprint.group(0),
                    "status": "ready",
                    "username": "openpiso-controller",
                }
                assert not contains_secret_key(state)

                users = router_rest(address, ROUTER_ADMIN, ROUTER_ADMIN_PASSWORD, "/user?name=openpiso-controller")
                assert len(users) == 1
                assert users[0]["group"] == "openpiso-controller"
                groups = router_rest(address, ROUTER_ADMIN, ROUTER_ADMIN_PASSWORD, "/user/group?name=openpiso-controller")
                assert len(groups) == 1
                policies = set(groups[0]["policy"].split(","))
                assert {"read", "write", "test", "api", "rest-api"} <= policies
                assert not ({"local", "telnet", "ssh", "ftp", "reboot", "policy", "password", "winbox", "web", "sniff", "sensitive", "romon"} & policies)
                report.snapshot("onboarded-with-special-character-admin-password", address, lab["CHR_PASSWORD"], opener,
                                password_contains_ampersand_equals_plus_hash_and_non_ascii=True,
                                admin_readback_write_only=True)

                use_existing(opener, report, address, lab["CHR_PASSWORD"], existing, tree_before_connect, other_before, users_before)
                if args.only != "onboarding":
                    existing.teardown_other()  # the onboarding scenario checks it again after a boot
                baseline_before_restart = capture_baseline(address, lab["CHR_PASSWORD"])
                response = request(opener, "/admin/router/probe", "POST", {
                    "address": f"https://{address}", "username": ROUTER_ADMIN, "password": ROUTER_ADMIN_PASSWORD,
                })
                assert response.status == 200
                response = request(opener, "/admin/router/confirm", "POST")
                assert response.status == 200, response.read().decode()
                assert capture_baseline(address, lab["CHR_PASSWORD"]) == baseline_before_restart
                report.snapshot("second-onboarding-is-idempotent", address, lab["CHR_PASSWORD"], opener)
                cleanup_router(lab["CHR_PASSWORD"])
            finally:
                report.capture_lab_failure()
                controller.stop()

            controller = Controller(args.binary, data_dir, initialize=False)
            try:
                cookies = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                response = request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD})
                assert response.status == 200
                response = request(opener, "/admin/state")
                state = json.load(response)
                assert state["router"]["status"] == "ready"
                baseline_after_restart = capture_baseline(address, lab["CHR_PASSWORD"])
                assert baseline_after_restart == baseline_before_restart
                report.snapshot("boot-reverified-with-bootstrap-admin-deleted", address, lab["CHR_PASSWORD"], opener)

                if args.only:
                    configure_device_routing(lab["CHR_PASSWORD"])
                    if args.only == "pairing":
                        _flash_module.scenario(sys.modules[__name__], opener, address, lab["CHR_PASSWORD"], client_address, report)
                    elif args.only == "vlan":
                        vendo_auto_select(opener, address, lab["CHR_PASSWORD"], client_address, report)
                    elif args.only == "sales":
                        device_redirect(client_address, lab["CHR_PASSWORD"])
                        sales_report(opener, report, address, lab["CHR_PASSWORD"], client_address)
                    elif args.only == "vendo-list":
                        vendo_list_details(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        device_redirect(client_address, lab["CHR_PASSWORD"])
                        device_ip = router_rest(client_address, "admin", lab["CHR_PASSWORD"],
                                                "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
                        vendo_list_rate_tier_validity(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                        vendo_list_points(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                        vendo_list_points_redeem(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                        vendo_list_hotspot_profile(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                        vendo_list_coins_out(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                        vendo_list_online_devices_up_since(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        vendo_list_deactivate(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        vendo_list_rates(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        vendo_list_points_tab(opener, report, address, lab["CHR_PASSWORD"], client_address)
                    elif args.only == "shop-defaults":
                        shop_defaults_rate_tiers(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        shop_defaults_vendo_list(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        shop_defaults_pill_lists(opener, report, address, lab["CHR_PASSWORD"], client_address)
                        shop_defaults_points_table(opener, report, address, lab["CHR_PASSWORD"], client_address)
                    elif args.only == "vouchers":
                        vouchers(opener, report, address, lab["CHR_PASSWORD"], client_address)
                    elif args.only == "onboarding":
                        controller, opener = onboarding(controller, args, data_dir, opener, report, address, lab["CHR_PASSWORD"],
                                                        client_address, existing, other_before)
                    elif args.only == "browser":
                        device_redirect(client_address, lab["CHR_PASSWORD"])
                        device_ip = router_rest(client_address, "admin", lab["CHR_PASSWORD"],
                                                "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
                        custom_portal(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                        _browser_module.scenario(sys.modules[__name__], opener, address, lab["CHR_PASSWORD"], client_address, report, restart_browser_controller)
                    else:  # time-running-out
                        device_redirect(client_address, lab["CHR_PASSWORD"])
                        device_ip = router_rest(client_address, "admin", lab["CHR_PASSWORD"],
                                                "/ip/address?interface=lan1", scheme="http")[0]["address"].split("/")[0]
                        response = generate_vouchers(opener, {"seconds": "15", "count": "1"})
                        assert response.status == 200
                        code = voucher_codes(opener, response)[0]
                        state = portal_state(pause_device_fetch(client_address, lab["CHR_PASSWORD"], device_ip,
                                                                "/portal/api/redeem", {"code": code}))
                        assert state["time_credit"]["bought"] == 15
                        time_running_out(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                    return

                controller.stop()
                drift_profile = router_rest(address, "admin", lab["CHR_PASSWORD"], "/ip/hotspot/profile?name=hs-prof")[0]
                router_rest(address, "admin", lab["CHR_PASSWORD"], f"/ip/hotspot/profile/{drift_profile['.id']}", "PATCH", {"login-by": "cookie,http-pap"})
                controller = Controller(args.binary, data_dir, initialize=False)
                cookies = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
                assert capture_baseline(address, lab["CHR_PASSWORD"]) == baseline_before_restart
                report.snapshot("boot-repairs-router-drift", address, lab["CHR_PASSWORD"], opener, repaired_login_by="http-pap")

                dashboard_clean_ledger(opener, report, address, lab["CHR_PASSWORD"])
                validity_settings(opener, report, address, lab["CHR_PASSWORD"])
                codes = voucher_batch(opener, report, address, lab["CHR_PASSWORD"])
                configure_device_routing(lab["CHR_PASSWORD"])
                client_address = start_client(lab["CHR_PASSWORD"])
                redirect_page, portal_page = device_redirect(client_address, lab["CHR_PASSWORD"])
                assert f"{PORTAL_URL}/portal" in redirect_page
                assert "02%3A00%3A00%3A00%3A0C%3A01" in redirect_page
                assert "OpenPiso WiFi" in portal_page and "/portal/core.js" in portal_page
                report.snapshot("customer-device-redirect-and-walled-garden", address, lab["CHR_PASSWORD"], opener,
                                redirect_html=redirect_page, portal_page=portal_page, customer_admin_status=403)
                paired = vendo_pairing(opener, address, lab["CHR_PASSWORD"], client_address, report)
                vendo = vendo_protocol(opener, paired, address, lab["CHR_PASSWORD"], report)
                before_restart = json.load(request(opener, "/admin/state"))["vendo_protocol"]
                repeated = vendo.frame()
                assert vendo.post(repeated, ignore_ack=True)[0] == 200
                controller.stop()
                controller = Controller(args.binary, data_dir, initialize=False)
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
                assert vendo.post(repeated)[0] == 200
                after_restart = json.load(request(opener, "/admin/state"))["vendo_protocol"]
                assert after_restart["coin_deltas"] == before_restart["coin_deltas"]
                assert after_restart["totals"] == before_restart["totals"]
                report.snapshot("vendo-restart-lost-ack-does-not-credit-twice", address, lab["CHR_PASSWORD"], opener)
                vendo_unpair(opener, paired, address, lab["CHR_PASSWORD"], report)
                renew_device_lease(client_address, lab["CHR_PASSWORD"])
                assert vendo.post(vendo.frame())[0] == 401
                device_ip, time_credit_cookie = voucher_redemption(opener, report, address, lab["CHR_PASSWORD"], client_address, codes)
                controller.stop()
                controller = Controller(args.binary, data_dir, initialize=False)
                cookies = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
                cookie_after_restart(client_address, lab["CHR_PASSWORD"], device_ip, time_credit_cookie)
                report.snapshot("time-credit-cookie-signing-key-persists-on-restart", address, lab["CHR_PASSWORD"], opener,
                                cookie_stable=True)
                controller, opener = reconciler_restart_outage(controller, args, data_dir, opener, report, address,
                                                                lab["CHR_PASSWORD"], client_address, device_ip)
                dashboard_router_unreachable(opener, report, address, lab["CHR_PASSWORD"])
                tapped_pause(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                idle_pause(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                top_up_code = top_up_online(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                top_up_pause_budget(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie, top_up_code)
                time_running_out(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                controller, opener = pending_credit_recovery(controller, args, data_dir, opener, report, address,
                                                             lab["CHR_PASSWORD"], client_address, device_ip)
                concurrent_voucher(opener, report, address, lab["CHR_PASSWORD"], client_address, codes[1])
                controller.stop()
                controller = Controller(args.binary, data_dir, initialize=False)
                cookies = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
                device_ip = rebind_recovery(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                admin_grant(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                admin_dashboard(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                controller, opener = admin_audit_review(controller, args, data_dir, opener, report, address,
                                                        lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                coin_window_empty(opener, report, address, lab["CHR_PASSWORD"], client_address)
                paired_coins, coin_vendo, coin_window = coin_window_open(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                coin_window_done(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, coin_vendo, coin_window)
                coin_window_grace(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, coin_vendo)
                controller, opener = coin_window_boot(controller, args, data_dir, opener, report, address,
                                                      lab["CHR_PASSWORD"], client_address, device_ip, coin_vendo)
                coin_window_idle(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, coin_vendo)
                device_ip = coin_window_departure(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, coin_vendo)
                resume_code_throttling(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                device_ip = mac_change_without_rebind(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                ambiguous_device_host(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, time_credit_cookie)
                expired_id, expired_cookie, validity_code = validity_online_expiry(
                    opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                admin_expired_grant(opener, report, address, lab["CHR_PASSWORD"], expired_id)
                validity_offline_expiry(opener, report, address, lab["CHR_PASSWORD"], client_address,
                                        device_ip, expired_id, expired_cookie, validity_code)
                # A Top-up never shortens Validity (ADR 0006), so the short-deadline
                # steps run while the Device holds no live Time Credit.
                admin_grant_after_deadline(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                coin_credit_after_expiry(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip, coin_vendo)
                voucher_while_tapped_paused(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                assert request(opener, "/admin/vendos/unpair", "POST", {"id": paired_coins["id"]}).status == 204
                sales_report(opener, report, address, lab["CHR_PASSWORD"], client_address)
                custom_portal(opener, report, address, lab["CHR_PASSWORD"], client_address, device_ip)
                _browser_module.scenario(sys.modules[__name__], opener, address, lab["CHR_PASSWORD"], client_address, report, restart_browser_controller)
                _flash_module.scenario(sys.modules[__name__], opener, address, lab["CHR_PASSWORD"], client_address, report)
                vendo_auto_select(opener, address, lab["CHR_PASSWORD"], client_address, report)
            finally:
                report.capture_lab_failure()
                controller.stop()

            configure_router_tls(address, lab["CHR_PASSWORD"])
            controller = Controller(args.binary, data_dir, initialize=False)
            try:
                cookies = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                assert request(opener, "/admin/login", "POST", {"password": ADMIN_PASSWORD}).status == 200
                state = json.load(request(opener, "/admin/state"))
                assert state["router"]["status"] == "error"
                assert "certificate" in state["router"]["error"].lower()
                assert state["router"]["fingerprint"] == fingerprint.group(0)
                report.snapshot("changed-certificate-refused", address, lab["CHR_PASSWORD"], opener)
            finally:
                report.capture_lab_failure()
                controller.stop()
    finally:
        # Finish all Router REST work before hot-unplugging its fixture NIC:
        # RouterOS management can be briefly unreachable during hot-unplug.
        report.capture_lab_failure()
        cleanup_steps(report, [
            ("coin board", lambda: cleanup_coin_board(client_address, lab["CHR_PASSWORD"])),
            ("Vendo Router rules", lambda: cleanup_vendo_router(address, lab["CHR_PASSWORD"])),
            ("HotSpot users", lambda: cleanup_hotspot_users(address, lab["CHR_PASSWORD"], original_hotspot_names)),
            ("existing HotSpot fixture", existing.teardown),
            ("bootstrap admin", lambda: cleanup_router(lab["CHR_PASSWORD"])),
            ("fixture Router address", lambda: rcon(lab["CHR_PASSWORD"], '/ip address remove [find where comment="openpiso-e2e-routing"]')),
            ("fixture source NAT", lambda: rcon(lab["CHR_PASSWORD"], '/ip firewall nat remove [find where comment="openpiso-e2e-routing"]')),
            ("fixture masquerade", lambda: rcon(lab["CHR_PASSWORD"], '/ip firewall nat remove [find where comment="openpiso-e2e-masquerade"]')),
            ("fixture Router NIC", detach_device_routing),
            ("fixture network", destroy_device_network),
        ])


def assert_arm_install(vm, report: Report) -> None:
    # Deployment evidence is from the guest OS; application behavior still uses HTTP.
    evidence = vm.install_evidence()
    assert evidence["machine"] == "armv7l", evidence
    assert 0 < evidence["memory_total_kib"] <= 256 * 1024, evidence
    assert evidence["swap_total_kib"] == 0, evidence
    assert evidence["service_user"] == "openpiso", evidence
    assert evidence["logs_persistent_capped"], evidence
    report.data["execution"] = evidence


# Partial scenarios for --only: each runs onboarding, then just that scenario, and
# writes `<name>-only.json` with scenario `<name>-only`. `boot` is the ARMv7 guest boot check.
ONLY_SCENARIOS = {
    "onboarding": "Router setup from someone else's HotSpot: Use existing, a Device online, the unticked server and a ledger from before Router setup",
    "vouchers": "Voucher batch generation, pricing snapshots, CSV and redemption",
    "pairing": "flash pairing",
    "vlan": "VLAN auto-select",
    "time-running-out": "time-running-out exhaustion",
    "sales": "the Sales report HTTP scenario",
    "browser": "the Custom Portal admin flows and the Portal browser E2E",
    "vendo-list": "the Vendo List, Rate tier, Points, HotSpot profile, Coins Out, Online Devices and Up since, Deactivate, Rates tab and Points tab scenarios",
    "shop-defaults": "the Shop › Rate tiers and Points table pages and the Vendo List's Shop wording scenarios",
    "boot": "with --armv7: boot the ARM guest through cloud-init and verify SSH, without CHR",
    "update": "with --armv7, without CHR: install a throwaway-key v0.1.0, then update, roll back and refuse releases (see update_vm.py)",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=ONLY_SCENARIOS,
                        help="run onboarding, then one scenario: " + "; ".join(f"{name}: {what}" for name, what in ONLY_SCENARIOS.items()))
    parser.add_argument("--build", action="store_true", help="build the Controller while holding the lab lock")
    parser.add_argument("--binary", help="Controller binary under test; --only update builds its own")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--write-vlan-hosts", action="store_true", help="refresh the committed VLAN auto-select research hosts.json")
    parser.add_argument("--armv7", action="store_true", help="run every scenario in a fresh 256 MiB ARM guest")
    parser.add_argument("--arm-boot-timeout", type=float, default=900, help="ARM boot/cloud-init timeout in seconds (default 900)")
    parser.add_argument("--http-timeout", type=float, help="HTTP timeout in seconds (native 3, ARM 120)")
    parser.add_argument("--lock-timeout", type=float, default=2700,
                        help="seconds to queue for the CHR lab lock before giving up (default 2700)")
    args = parser.parse_args()
    if not 0 < args.arm_boot_timeout < float("inf"):
        parser.error("--arm-boot-timeout must be a positive finite number")
    if not 0 < args.lock_timeout < float("inf"):
        parser.error("--lock-timeout must be a positive finite number")
    if args.only in ("boot", "update") and not args.armv7:
        parser.error(f"--only {args.only} requires --armv7")
    if args.binary is None and args.only != "update":
        parser.error("--binary is required")
    with _safety_module.lab_lock(args.lock_timeout):
        run(args)


def run(args: argparse.Namespace) -> None:
    global ARM_VM, BASE_URL, PORTAL_URL, HTTP_TIMEOUT
    boot_only = args.only == "boot"
    update_only = args.only == "update"
    no_lab = boot_only or update_only  # ARM guest only, no CHR
    HTTP_TIMEOUT = args.http_timeout if args.http_timeout is not None else (120 if args.armv7 else 3)
    partial_mode = f"armv7-{args.only}" if no_lab else (f"{args.only}-only" if args.only else None)
    if args.report is None:
        name = f"{partial_mode}.json" if no_lab else ("armv7.json" if args.armv7 else "walking-skeleton.json")
        if not args.armv7 and partial_mode:
            name = f"{partial_mode}.json"
        args.report = Path(__file__).parent / "reports" / name
    releases = None
    if update_only:
        spec = importlib.util.spec_from_file_location("update_vm", Path(__file__).with_name("update_vm.py"))
        update_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(update_module)
        releases = update_module.Releases()  # builds the throwaway-key releases, v0.1.0 first
        args.binary = str(releases.binary("v0.1.0"))
    elif args.build:
        subprocess.run(["make", "-C", str(ROOT / "controller"), "armv7" if args.armv7 else "build"], check=True)
    if not no_lab:
        lab = load_lab_env()
        _safety_module.preflight(ROOT)
    report = Report(args.binary, args.report)
    if not no_lab:
        report.lab_snapshot = lambda: _lab_module.snapshot(
            lambda path: router_rest(router_address("chr-client"), "admin", lab["CHR_PASSWORD"], path, scheme="http"),
            lambda path: router_rest(router_address(), "admin", lab["CHR_PASSWORD"], path))
    if partial_mode:
        report.data["scenario"] = partial_mode
    try:
        if args.armv7:
            spec = importlib.util.spec_from_file_location("arm_vm", Path(__file__).with_name("arm_vm.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            ARM_VM = module.ArmVM(Path(args.binary), args.arm_boot_timeout, args.report.with_suffix(".serial.log"))
            ARM_VM.boot()
            BASE_URL = PORTAL_URL = f"http://{ARM_VM.address}:18080"
            if boot_only:
                evidence = ARM_VM.boot_evidence()
                assert evidence["machine"] == "armv7l", evidence
                assert 0 < evidence["memory_total_kib"] <= 256 * 1024, evidence
                assert evidence["swap_total_kib"] == 0, evidence
                report.data["execution"] = evidence
            elif update_only:
                releases.run(ARM_VM, report, ADMIN_PASSWORD)
            else:
                ARM_VM.route_customer_network(router_address())
                ARM_VM.install(ADMIN_PASSWORD)
                assert_arm_install(ARM_VM, report)
        if not no_lab:
            scenario(args, report, lab)
        if ARM_VM is not None and not no_lab:
            peak = ARM_VM.peak_rss_kib()
            report.data["execution"]["peak_controller_rss_kib"] = peak
            report.data["execution"]["rss_target_kib"] = 64 * 1024
            assert 0 < peak < 64 * 1024, f"Controller peak RSS {peak} KiB; target <65536 KiB"
    except Exception as error:
        report.capture_lab_failure()
        if ARM_VM is not None:
            report.data.setdefault("execution", {}).update(ARM_VM.failure_evidence())
        report.finish("fail", str(error))
        raise
    else:
        report.finish("pass")
    finally:
        if ARM_VM is not None:
            ARM_VM.close()
        if releases is not None:
            releases.close()


if __name__ == "__main__":
    main()
