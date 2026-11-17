#!/usr/bin/env python3
"""Persistent local dev lab: a dev-build Controller on the shared chr-lab Router, with a
browser standing in for the customer Device. See lab/dev/README.md.

Run: python3 -I lab/dev/dev.py {up,vendo,phone,shots,admin-shots,down}
"""

import argparse
import fcntl
import http.cookiejar
import importlib.util
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "lab/dev/.run"
VIRSH = ["virsh", "-c", "qemu:///system"]

CONTROLLER = "http://127.0.0.1"  # admin and Vendo traffic, from the host
PORTAL_URL = "http://192.168.122.1"  # what Devices use, through the walled garden
RATES = "1=60\n5=360"  # seeded only when the Controller has no rate tiers

# MAC/IP plan on the HotSpot LAN (10.5.50.0/24, DHCP pool .10-.200). Locally administered MACs.
# Device IPs must sit inside the pool: the HotSpot remaps any other host IP to a pool address
# (`to-address`), so the Controller would never find the Device. A static DHCP lease reserves each one.
VENDO_MAC, VENDO_IP = "02:00:00:0D:00:F0", "10.5.50.20"  # pool's low end: DHCP hands out from the top
PHONE_MAC, PHONE_IP = "02:00:00:0d:00:03", "10.5.50.190"  # `phone` netns
SHOTS_IPS = range(150, 170)  # `shots` containers, one random MAC/IP per profile per run
LEASE_COMMENT = "openpiso-dev-device"
HS_NETWORK = "openpiso-dev-hs"  # docker macvlan on virbr-hs

# The Router reaches the Controller's host, but replies to 10.5.50.0/24 need a route. libvirt installs
# one without sudo from a throwaway network plus a third chr-lab NIC (as controller/e2e/run.py does).
ROUTING_NET, ROUTING_BRIDGE, ROUTING_SUBNET = "openpiso-dev-routing", "virbr-opdev", "10.77.8"
ROUTING_MAC = "52:54:00:07:08:07"
ROUTING_COMMENT = "openpiso-dev-routing"

# The official image ships the browsers but not the Python package; a local layer adds it at the same version.
PLAYWRIGHT_BASE = "mcr.microsoft.com/playwright/python:v1.64.1-noble"
PLAYWRIGHT_IMAGE = "openpiso-dev-shots:pw1.64.1"
PROFILES = {"iphone-15": "iPhone 15", "pixel-7": "Pixel 7"}  # Playwright device descriptors


def load(path: str, name: str):
    # python -I omits the script directory from sys.path, so import by path.
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e2e = load("controller/e2e/run.py", "e2e_run")  # router_rest, rcon, configure_router_tls, ROUTER_ADMIN*
e2e.BASE_URL = CONTROLLER  # e2e.request and e2e.wait_for address the dev Controller
SimulatedVendo = e2e.SimulatedVendo
router_rest = e2e.router_rest


def say(message: str) -> None:
    print(f"[dev] {message}", flush=True)


def die(message: str) -> None:
    sys.exit(f"[dev] {message}")


def chr_password() -> str:
    """CHR admin password from lab/chr/.env, here or in the main worktree. Never printed."""
    candidates = [ROOT / "lab/chr/.env"]
    common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True)
    if common.returncode == 0:
        candidates.append(Path(common.stdout.strip()).parent / "lab/chr/.env")
    for path in candidates:
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.startswith("CHR_PASSWORD="):
                    return line.split("=", 1)[1].strip("'\"")
    die("lab/chr/.env with CHR_PASSWORD not found (run lab/chr/wizard.sh, or use the main checkout's)")


def virsh(*args: str, check: bool = True) -> str:
    result = subprocess.run([*VIRSH, *args], capture_output=True, text=True, timeout=60)
    if check and result.returncode != 0:
        die(f"virsh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    if check and result.returncode != 0:
        die(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def ensure_vm(password: str) -> str:
    """Start chr-lab if it is off and return its management address once REST answers."""
    if virsh("domstate", "chr-lab") != "running":
        say("starting chr-lab")
        virsh("start", "chr-lab")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        match = re.search(r"(192\.168\.122\.\d+)/24", virsh("domifaddr", "chr-lab", check=False))
        if match:
            try:
                router_rest(match.group(1), "admin", password, "/system/resource", scheme="http")
                return match.group(1)
            except OSError:
                pass
        time.sleep(2)
    die("chr-lab did not come up with a management address")


def ensure_dns(address: str, password: str) -> None:
    """The HotSpot answers Device DNS through the Router resolver, which refuses LAN clients by default."""
    if router_rest(address, "admin", password, "/ip/dns")["allow-remote-requests"] != "true":
        router_rest(address, "admin", password, "/ip/dns/set", "POST", {"allow-remote-requests": "yes"})
        say("enabled /ip dns allow-remote-requests on the Router")


def ensure_routing(address: str, password: str) -> None:
    """Return route from the host to the HotSpot LAN, so the Controller sees Devices' real IPs."""
    if ROUTING_NET not in virsh("net-list", "--name").split():
        xml = (f'<network><name>{ROUTING_NET}</name><bridge name="{ROUTING_BRIDGE}"/>'
               f'<ip address="{ROUTING_SUBNET}.1" netmask="255.255.255.0"/>'
               f'<route address="10.5.50.0" prefix="24" gateway="{ROUTING_SUBNET}.2"/></network>')
        subprocess.run([*VIRSH, "net-create", "/dev/stdin"], input=xml, text=True, check=True, capture_output=True)
    if ROUTING_MAC not in virsh("domiflist", "chr-lab"):
        virsh("attach-interface", "chr-lab", "network", ROUTING_NET, "--model", "virtio", "--mac", ROUTING_MAC, "--live")
    deadline = time.monotonic() + 30
    while True:
        nics = router_rest(address, "admin", password, f"/interface/ethernet?mac-address={ROUTING_MAC}")
        if nics:
            break
        if time.monotonic() > deadline:
            die("chr-lab did not show the routing NIC")
        time.sleep(1)
    if not router_rest(address, "admin", password, f"/ip/address?comment={ROUTING_COMMENT}"):
        router_rest(address, "admin", password, "/ip/address", "PUT", {
            "address": f"{ROUTING_SUBNET}.2/24", "interface": nics[0]["name"], "comment": ROUTING_COMMENT})
    # Accept before the masquerade rule, or the Controller would see the Router's address instead.
    if not router_rest(address, "admin", password, f"/ip/firewall/nat?comment={ROUTING_COMMENT}"):
        router_rest(address, "admin", password, "/ip/firewall/nat", "PUT", {
            "chain": "srcnat", "src-address": "10.5.50.0/24", "dst-address": "192.168.122.1",
            "action": "accept", "place-before": "0", "comment": ROUTING_COMMENT})


def remove_routing(password: str) -> None:
    domain = virsh("domstate", "chr-lab", check=False)
    if domain == "running":
        match = re.search(r"(192\.168\.122\.\d+)/24", virsh("domifaddr", "chr-lab", check=False))
        if match:
            address = match.group(1)
            for path in ("/ip/address", "/ip/firewall/nat"):
                try:
                    for entry in router_rest(address, "admin", password, f"{path}?comment={ROUTING_COMMENT}"):
                        router_rest(address, "admin", password, f"{path}/{entry['.id']}", "DELETE")
                except OSError as error:
                    say(f"could not clean {path} on the Router: {error}")
        if ROUTING_MAC in virsh("domiflist", "chr-lab", check=False):
            virsh("detach-interface", "chr-lab", "network", "--mac", ROUTING_MAC, "--live", check=False)
    if ROUTING_NET in virsh("net-list", "--name", check=False).split():
        virsh("net-destroy", ROUTING_NET, check=False)


def forget_device(address: str, password: str, mac: str, ip: str) -> None:
    """Remove a Device's HotSpot host, session and static lease. A stale host makes its IP ambiguous."""
    mac = mac.upper()  # RouterOS queries are case-sensitive
    for path, query in (("/ip/hotspot/host", f"address={ip}"), ("/ip/hotspot/host", f"mac-address={mac}"),
                        ("/ip/hotspot/active", f"mac-address={mac}"), ("/ip/dhcp-server/lease", f"mac-address={mac}"),
                        ("/ip/dhcp-server/lease", f"address={ip}&comment={LEASE_COMMENT}")):
        for entry in router_rest(address, "admin", password, f"{path}?{query}"):
            router_rest(address, "admin", password, f"{path}/{entry['.id']}", "DELETE")


def reserve_device(address: str, password: str, mac: str, ip: str) -> None:
    """Start a Device from scratch: drop leftovers, then reserve its in-pool IP with a static lease."""
    forget_device(address, password, mac, ip)
    router_rest(address, "admin", password, "/ip/dhcp-server/lease", "PUT", {
        "address": ip, "mac-address": mac, "server": "hs-dhcp", "comment": LEASE_COMMENT})


def router_tls_ok(address: str, password: str) -> bool:
    try:
        service = router_rest(address, "admin", password, "/ip/service?name=www-ssl")[0]
        cert = router_rest(address, "admin", password, "/certificate?name=openpiso-e2e-server")[0]
    except (OSError, IndexError):
        return False
    return (service["disabled"] == "false" and service.get("certificate") == "openpiso-e2e-server"
            and f"IP:{address}" in cert.get("subject-alt-name", "")
            and cert.get("expired") != "true" and cert.get("invalid") != "true")


class Admin:
    """Admin session on the dev Controller."""

    def __init__(self, password: str):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.post("/admin/login", {"password": password})

    def request(self, path: str, form=None) -> tuple[int, str]:
        # e2e.request returns the HTTPError itself on an error status; both are readable responses.
        with e2e.request(self.opener, path, "POST" if form is not None else "GET", form) as response:
            return response.status, response.read().decode()

    def post(self, path: str, form: dict) -> str:
        status, body = self.request(path, form)
        if status not in (200, 201, 204):
            die(f"POST {path} -> {status}: {body.strip()[:300]}")
        return body

    def state(self) -> dict:
        return json.loads(self.request("/admin/state")[1])


def onboard_router(admin: Admin, address: str, password: str) -> None:
    """Pin the Router's current certificate, as the operator does on the Router page.

    An E2E run leaves openpiso-controller with its own secret, and re-onboarding never resets an
    existing user's password (routeros.Onboard), so drop the user and let the Controller recreate it."""
    def drop_user(name: str) -> None:
        for user in router_rest(address, "admin", password, f"/user?name={name}"):
            router_rest(address, "admin", password, f"/user/{user['.id']}", "DELETE")

    drop_user("openpiso-controller")
    drop_user(e2e.ROUTER_ADMIN)
    router_rest(address, "admin", password, "/user", "PUT", {
        "name": e2e.ROUTER_ADMIN, "group": "full", "password": e2e.ROUTER_ADMIN_PASSWORD})
    try:
        admin.post("/admin/router/probe", {"address": f"https://{address}", "username": e2e.ROUTER_ADMIN,
                                           "password": e2e.ROUTER_ADMIN_PASSWORD})
        admin.post("/admin/router/confirm", {})
    finally:
        drop_user(e2e.ROUTER_ADMIN)


def ensure_vendo(admin: Admin, address: str, password: str) -> dict:
    """Exactly one live dev Vendo; its id/secret/epoch live in .run/vendo.json. Pairing only provisions
    it (flash-time pairing): it stays provisioned or pending until adopt_vendo's first Report."""
    path = RUN / "vendo.json"
    saved = json.loads(path.read_text()) if path.exists() else None
    live = [v for v in admin.state().get("vendos") or [] if v["status"] != "unpaired"]
    if saved and [v["id"] for v in live] == [saved["id"]]:
        return saved
    for vendo in live:  # secret lost, or another Vendo: start clean
        admin.post("/admin/vendos/unpair", {"id": vendo["id"]})
    e2e.cleanup_vendo_router(address, password)  # stale openpiso-vendo-* rules from other runs
    result = json.loads(admin.post("/admin/vendos/pair", {"mac": VENDO_MAC}))
    saved = {"id": result["id"], "secret": result["secret"], "mac": VENDO_MAC, "ip": VENDO_IP,
             "epoch": saved["epoch"] if saved else 0}
    write_private(path, json.dumps(saved))
    say(f"provisioned dev Vendo {saved['id']} ({VENDO_MAC})")
    return saved


# One board boot on the HotSpot LAN: Report until the Controller acks (pairing resets the first connection).
FIRST_REPORT = """
import json, sys, time
sys.path.insert(0, "/work")
from simulated_vendo import SimulatedVendo
state = json.load(open("/work/vendo.json"))
board = SimulatedVendo(sys.argv[1], state["id"], bytes.fromhex(state["secret"]), state["epoch"])
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    try:
        status, body = board.post(board.frame())
    except OSError as error:
        status, body = 0, str(error).encode()
    print(status, body[:80], flush=True)
    if status == 200:
        sys.exit(0)
    time.sleep(1)
sys.exit(1)
"""


def adopt_vendo(admin: Admin, address: str, password: str) -> None:
    """Finish the dev Vendo's pairing: the Controller pairs a Vendo on its first Report, and only from the
    HotSpot host with its MAC, so a container on the HotSpot LAN sends it. Later Reports may come from the host."""
    with vendo_boot() as state:
        status = lambda: next(v["status"] for v in admin.state()["vendos"] if v["id"] == state["id"])
        if status() == "paired":
            return
        ensure_image()
        ensure_hs_network()
        forget_device(address, password, VENDO_MAC, VENDO_IP)
        # Without a lease the HotSpot remaps the IP and the Controller can't match the MAC. PairVendo adopts a
        # static lease carrying its own comment, and re-provisioning's cleanup removes it.
        router_rest(address, "admin", password, "/ip/dhcp-server/lease", "PUT", {
            "address": VENDO_IP, "mac-address": VENDO_MAC, "server": "hs-dhcp", "comment": f"openpiso-vendo-{state['id']}"})
        say(f"pairing dev Vendo {state['id']}: first Report from {VENDO_MAC} {VENDO_IP}")
        run = subprocess.run(
            ["docker", "run", "--rm", "--name", "openpiso-dev-vendo", "--label", "openpiso-dev=shots",
             "--network", HS_NETWORK, "--ip", VENDO_IP, "--mac-address", VENDO_MAC,
             "--user", f"{os.getuid()}:{os.getgid()}",
             "-v", f"{ROOT / 'controller/e2e/simulated_vendo.py'}:/work/simulated_vendo.py:ro",
             "-v", f"{RUN / 'vendo.json'}:/work/vendo.json:ro",
             PLAYWRIGHT_IMAGE, "python", "-I", "-c", FIRST_REPORT, PORTAL_URL],
            capture_output=True, text=True, timeout=90)
        if status() != "paired":
            die(f"dev Vendo {state['id']} is {status()} after its first Report:\n{(run.stdout + run.stderr).strip()[-600:]}")
        say(f"paired dev Vendo {state['id']}")


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)


def build_controller() -> Path:
    go = shutil.which("go") or str(Path.home() / ".local/go/bin/go")
    binary = RUN / "openpiso-dev"
    say("building the Controller (-tags dev)")
    result = subprocess.run([go, "build", "-tags", "dev", "-o", str(binary), "./cmd/openpiso"],
                            cwd=ROOT / "controller", env={**os.environ, "CGO_ENABLED": "0"})
    if result.returncode != 0:
        die("go build failed")
    # Port 80 needs cap_net_bind_service. An up-to-date build only touches the binary, so the
    # capability survives restarts; a real rebuild rewrites it and clears the capability.
    caps = subprocess.run(["getcap", str(binary)], capture_output=True, text=True).stdout
    if "cap_net_bind_service" not in caps:
        say("granting the dev binary cap_net_bind_service with sudo (port 80 is below 1024)")
        if subprocess.run(["sudo", "setcap", "cap_net_bind_service=+ep", str(binary)]).returncode != 0:
            die("setcap failed: the Controller cannot bind port 80 without cap_net_bind_service")
    return binary


def cmd_up(_args) -> None:
    password = chr_password()
    RUN.mkdir(mode=0o700, exist_ok=True)
    try:
        urllib.request.urlopen(CONTROLLER + "/healthz", timeout=1)
        die("a Controller already answers on 127.0.0.1:80; stop it first")
    except OSError:
        pass
    address = ensure_vm(password)
    say(f"chr-lab is up at {address}")
    if not router_tls_ok(address, password):  # every later REST call is HTTPS
        say("generating the Router's www-ssl certificate")
        e2e.configure_router_tls(address, password)
    ensure_dns(address, password)
    ensure_routing(address, password)
    binary = build_controller()

    admin_file = RUN / "admin-password"
    fresh = not admin_file.exists()
    if fresh:
        write_private(admin_file, secrets.token_urlsafe(12))
    admin_password = admin_file.read_text().strip()
    log = open(RUN / "controller.log", "a", buffering=1)
    controller = subprocess.Popen(
        [str(binary), "-listen", "0.0.0.0:80", "-data", str(RUN / "data"), "-portal-url", PORTAL_URL],
        env={**os.environ, "OPENPISO_ADMIN_PASSWORD": admin_password},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stream() -> None:
        for line in controller.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)

    threading.Thread(target=stream, daemon=True).start()
    try:
        e2e.wait_for(CONTROLLER + "/healthz", timeout=30)
        admin = Admin(admin_password)
        router = admin.state().get("router") or {}
        if router.get("status") != "ready" or router.get("address") != f"https://{address}":
            # Every E2E run regenerates the Router certificate, so a stale pin here is expected.
            say(f"Router onboarding is {router.get('status') or 'missing'} ({router.get('error') or 'no error'}); "
                f"pinning {address} (E2E runs regenerate the Router certificate)")
            onboard_router(admin, address, password)
            router = admin.state().get("router") or {}
            if router.get("status") != "ready":
                die(f"Router is not ready after onboarding: {router}")
        ensure_vendo(admin, address, password)
        adopt_vendo(admin, address, password)
        if not admin.state().get("rate_tiers"):
            admin.post("/admin/rates", {"tiers": RATES})
            say(f"seeded Coin rates: {RATES.replace(chr(10), ', ')}")
        print(f"""
Dev lab is up.
  Admin:     {CONTROLLER}/admin/login  (password in {admin_file}{f': {admin_password}' if fresh else ''})
  Portal:    {PORTAL_URL}/portal  (only reachable by Devices behind the HotSpot)
  Next:      python3 -I lab/dev/dev.py vendo   # coin slot for the dev Vendo
             python3 -I lab/dev/dev.py phone   # Firefox as a Device (sudo)
             python3 -I lab/dev/dev.py shots   # scripted phones, screenshots
Template/CSS/JS edits show on refresh. After Go changes press Ctrl+C and run `up` again.
Streaming the Controller log; Ctrl+C stops the Controller only.
""", flush=True)
        controller.wait()
        die(f"Controller exited with {controller.returncode}")
    except KeyboardInterrupt:
        pass
    finally:
        if controller.poll() is None:
            controller.terminate()
            try:
                controller.wait(timeout=5)
            except subprocess.TimeoutExpired:
                controller.kill()
        say("Controller stopped; chr-lab stays up")


@contextmanager
def vendo_boot():
    """Take the Vendo pidfile lock and bump the persisted epoch: each holder is one board boot."""
    path = RUN / "vendo.json"
    if not path.exists():
        die("no dev Vendo yet: run `up` first")
    lock = open(RUN / "vendo.pid", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        die(f"another simulator already runs the dev Vendo (pid {lock.read().strip()})")
    lock.truncate(0)
    lock.write(str(os.getpid()))
    lock.flush()
    try:
        state = json.loads(path.read_text())
        state["epoch"] += 1
        write_private(path, json.dumps(state))
        yield state
    finally:
        lock.truncate(0)
        lock.close()


def release_windows(state: dict) -> None:
    """Let this boot report Closed for Coin Windows an earlier run left open: a new boot starts without
    the window id, and the Controller keeps the Vendo busy until a Closed names it."""
    admin = Admin((RUN / "admin-password").read_text().strip())
    board = SimulatedVendo(CONTROLLER, state["id"], bytes.fromhex(state["secret"]), state["epoch"])
    for window in admin.state()["coin_windows"]:
        if window["vendo_id"] == state["id"] and not window["closed_at"]:
            board.window_id = window["id"]
            board.post(board.frame(5))
            say(f"released stuck Coin Window {window['id']}")


def cmd_vendo(_args) -> None:
    with vendo_boot() as state:
        vendo = SimulatedVendo(CONTROLLER, state["id"], bytes.fromhex(state["secret"]), state["epoch"])
        guard = threading.Lock()
        stop = threading.Event()
        last = {"window": 0, "closing": False, "down": False}

        def heartbeat() -> None:
            try:
                vendo.heartbeat()
                last["down"] = False
            except (OSError, AssertionError) as error:
                if not last["down"]:
                    print(f"\n[vendo] Controller unreachable: {error}", flush=True)
                last["down"] = True
                return
            if (vendo.window_id, vendo.closing) != (last["window"], last["closing"]):
                last["window"], last["closing"] = vendo.window_id, vendo.closing
                print(f"\n[vendo] window {vendo.window_id or 'none'}{' (closing)' if vendo.closing else ''}, "
                      f"total {vendo.total}", flush=True)

        def loop() -> None:
            while not stop.is_set():
                with guard:
                    heartbeat()
                stop.wait(1 if vendo.window_id else 2)

        release_windows(state)
        say(f"Vendo {state['id']} boot, epoch {vendo.epoch}. Type a number of pulses + Enter; q quits.")
        threading.Thread(target=loop, daemon=True).start()
        try:
            for line in sys.stdin:
                text = line.strip()
                if text == "q":
                    break
                if not text.isdigit() or int(text) == 0:
                    print("enter a positive number of pulses, or q")
                    continue
                with guard:
                    try:
                        vendo.insert(int(text))
                    except ValueError:
                        print("acceptor inhibited: open a Coin Window from the portal first")
                        continue
                    heartbeat()
                    print(f"[vendo] +{text}: window {vendo.window_id}, total {vendo.total}", flush=True)
        except KeyboardInterrupt:
            pass
        stop.set()


def cmd_phone(_args) -> None:
    """Firefox in netns `devphone` on virbr-hs. Needs sudo (typed by the user); unverified in CI."""
    profile = RUN / "firefox-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "user.js").write_text(
        'user_pref("network.trr.mode", 5);\n'  # DoH off, so DNS goes to the HotSpot
        'user_pref("network.captive-portal-service.enabled", true);\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n')
    address, password = e2e.router_address(), chr_password()
    if not router_rest(address, "admin", password, f"/ip/dhcp-server/lease?mac-address={PHONE_MAC.upper()}"):
        router_rest(address, "admin", password, "/ip/dhcp-server/lease", "PUT", {
            "address": PHONE_IP, "mac-address": PHONE_MAC, "server": "hs-dhcp", "comment": LEASE_COMMENT})
    setup = f"""set -e
ip netns list | grep -qw devphone || ip netns add devphone
ip link show vDP-br >/dev/null 2>&1 || ip link add vDP-br type veth peer name vDP
ip link set vDP-br master virbr-hs
ip link set vDP-br up
ip link show vDP >/dev/null 2>&1 && ip link set vDP netns devphone
ip -n devphone link set lo up
ip -n devphone link set vDP address {PHONE_MAC}
ip -n devphone link set vDP up
ip -n devphone addr replace {PHONE_IP}/24 dev vDP
ip -n devphone route replace default via 10.5.50.1
mkdir -p /etc/netns/devphone
echo 'nameserver 10.5.50.1' > /etc/netns/devphone/resolv.conf
"""
    say(f"creating netns devphone ({PHONE_MAC} {PHONE_IP}) with sudo")
    if subprocess.run(["sudo", "bash", "-c", setup]).returncode != 0:
        die("netns setup failed")
    env = [f"{name}={os.environ[name]}" for name in
           ("HOME", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY")
           if name in os.environ]
    env.append("MOZ_ENABLE_WAYLAND=1")
    firefox = ["sudo", "ip", "netns", "exec", "devphone", "sudo", "-u", os.environ["USER"], "env", *env,
               "firefox", "--no-remote", "--new-instance", "--profile", str(profile), "http://neverssl.com/"]
    say("launching Firefox as a customer Device; close it to return")
    subprocess.run(firefox)


def ensure_image() -> None:
    if docker("image", "inspect", PLAYWRIGHT_IMAGE, check=False).returncode != 0:
        say(f"building {PLAYWRIGHT_IMAGE} (one time)")
        dockerfile = f"FROM {PLAYWRIGHT_BASE}\nRUN pip install --no-cache-dir --break-system-packages playwright==1.64.1\n"
        subprocess.run(["docker", "build", "-q", "-t", PLAYWRIGHT_IMAGE, "-"], input=dockerfile, text=True, check=True)


def ensure_hs_network() -> None:
    if docker("network", "inspect", HS_NETWORK, check=False).returncode != 0:
        docker("network", "create", "-d", "macvlan", "--subnet", "10.5.50.0/24", "--gateway", "10.5.50.1",
               "-o", "parent=virbr-hs", HS_NETWORK)


def cmd_shots(args) -> None:
    for name in args.profile:
        if name not in PROFILES:
            die(f"unknown profile {name}; known: {', '.join(PROFILES)}")
    try:
        urllib.request.urlopen(CONTROLLER + "/healthz", timeout=2)
    except OSError:
        die("no Controller on 127.0.0.1:80: run `up` first")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUN / "shots" / stamp
    report = {"started": stamp, "profiles": []}
    ensure_image()
    ensure_hs_network()
    password = chr_password()
    router = e2e.router_address()
    ensure_routing(router, password)  # `down` removes it; shots must not need `up` again for this
    with vendo_boot() as state:
        release_windows(state)
        try:
            octets = random.sample(SHOTS_IPS, len(args.profile))
            for name, octet in zip(args.profile, octets):
                mac = "02:de:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(4))
                ip = f"10.5.50.{octet}"
                profile_dir = out / name
                profile_dir.mkdir(parents=True)
                say(f"{name}: {PROFILES[name]} as {mac} {ip}")
                # Each container is one board boot: a new epoch, so its totals start from 0 again.
                state["epoch"] += 1
                write_private(RUN / "vendo.json", json.dumps(state))
                write_private(out / "vendo.json", json.dumps(state))
                reserve_device(router, password, mac, ip)
                started = time.monotonic()
                stdout = stderr = ""
                try:
                    run = subprocess.run(
                        ["docker", "run", "--rm", "--init", "--ipc=host", "--name", f"openpiso-dev-shots-{name}",
                         "--label", "openpiso-dev=shots", "--network", HS_NETWORK, "--ip", ip, "--mac-address", mac,
                         "--dns", "10.5.50.1", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                         "-v", f"{ROOT / 'lab/dev/shots.py'}:/work/shots.py:ro",
                         "-v", f"{ROOT / 'controller/e2e/simulated_vendo.py'}:/work/simulated_vendo.py:ro",
                         "-v", f"{out / 'vendo.json'}:/work/vendo.json:ro",
                         "-v", f"{profile_dir}:/out", PLAYWRIGHT_IMAGE,
                         "python", "/work/shots.py", "--device", PROFILES[name], "--controller", PORTAL_URL,
                         "--vendo", "/work/vendo.json", "--out", "/out"],
                        capture_output=True, text=True, timeout=300)
                    stdout, stderr = run.stdout, run.stderr
                    reason = "flow did not finish"
                except subprocess.TimeoutExpired:
                    docker("rm", "-f", f"openpiso-dev-shots-{name}", check=False)
                    reason = "timed out after 300 s"
                finally:
                    forget_device(router, password, mac, ip)
                result = profile_dir / "result.json"
                entry = json.loads(result.read_text()) if result.exists() else {
                    "result": "fail", "error": reason, "steps": [], "stderr": stderr[-2000:]}
                report["profiles"].append({"profile": name, "device": PROFILES[name], "mac": mac, "ip": ip,
                                           "seconds": round(time.monotonic() - started, 1), **entry})
                print(stdout, end="")
                status = entry["result"]
                say(f"{name}: {status}" + ("" if status == "pass" else f" ({entry.get('error')})"))
                if status != "pass" and stderr.strip():
                    print(stderr[-1500:], file=sys.stderr)
        finally:
            (out / "vendo.json").unlink(missing_ok=True)  # holds the Vendo secret
            write_private(out / "report.json", json.dumps(report, indent=2))
    if any(p["result"] != "pass" for p in report["profiles"]):
        sys.exit(1)


def cmd_admin_shots(args) -> None:
    """Admin panel screenshots from fixture data: no Router, no sudo, no port 80."""
    go = shutil.which("go") or str(Path.home() / ".local/go/bin/go")
    binary = RUN / "adminshots"
    say("building adminshots (-tags dev)")
    if subprocess.run([go, "build", "-tags", "dev", "-o", str(binary), "./cmd/adminshots"],
                      cwd=ROOT / "controller", env={**os.environ, "CGO_ENABLED": "0"}).returncode != 0:
        die("go build failed")
    ensure_image()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    servers = [subprocess.Popen([str(binary), *flags], stdout=subprocess.PIPE, text=True) for flags in ([], ["-empty"])]
    try:
        # Each prints one JSON line once seeded; the populated one waits ~11 s for its Offline Vendos.
        ready = [server.stdout.readline() for server in servers]
        if not all(ready):
            die("adminshots exited before it was ready")
        populated, empty = (line.strip() for line in ready)
        say(f"serving fixture at {json.loads(populated)['base']}, empty at {json.loads(empty)['base']}")
        run = subprocess.run(
            ["docker", "run", "--rm", "--init", "--ipc=host", "--name", "openpiso-dev-admin-shots",
             "--label", "openpiso-dev=shots", "--network", "host",
             "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
             "-v", f"{ROOT / 'lab/dev/admin_shots.py'}:/work/admin_shots.py:ro", "-v", f"{out}:/out",
             PLAYWRIGHT_IMAGE, "python", "-I", "/work/admin_shots.py",
             "--populated", populated, "--empty", empty, "--out", "/out"], timeout=900)
    finally:
        for server in servers:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
    shots = [p for p in out.glob("*.png") if p.stat().st_mtime >= started]
    say(f"{len(shots)} PNGs in {out}")
    if run.returncode != 0:
        die("some admin states failed (see FAIL lines above)")


def cmd_down(args) -> None:
    ids = docker("ps", "-aq", "--filter", "label=openpiso-dev=shots").stdout.split()
    if ids:
        docker("rm", "-f", *ids)
    if docker("network", "inspect", HS_NETWORK, check=False).returncode == 0:
        docker("network", "rm", HS_NETWORK)
    password = chr_password()
    if virsh("domstate", "chr-lab", check=False) == "running":
        router = e2e.router_address()
        for lease in router_rest(router, "admin", password, f"/ip/dhcp-server/lease?comment={LEASE_COMMENT}"):
            router_rest(router, "admin", password, f"/ip/dhcp-server/lease/{lease['.id']}", "DELETE")
    remove_routing(password)
    if args.vm:
        virsh("shutdown", "chr-lab", check=False)
        say("chr-lab shutdown requested")
    say("down: shots containers, macvlan network and routing fixture removed" + ("" if args.vm else "; chr-lab left running (down --vm shuts it down)"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="start the dev Controller and make the lab healthy").set_defaults(run=cmd_up)
    commands.add_parser("vendo", help="interactive coin slot for the dev Vendo").set_defaults(run=cmd_vendo)
    commands.add_parser("phone", help="Firefox as a customer Device in netns devphone (sudo)").set_defaults(run=cmd_phone)
    shots = commands.add_parser("shots", help="scripted phones through the HotSpot, with screenshots")
    shots.add_argument("profile", nargs="*", default=list(PROFILES), help=f"default: {' '.join(PROFILES)}")
    shots.set_defaults(run=cmd_shots)
    admin_shots = commands.add_parser("admin-shots", help="admin panel screenshots from fixture data (no Router)")
    admin_shots.add_argument("--out", required=True, help="directory for the PNGs")
    admin_shots.set_defaults(run=cmd_admin_shots)
    down = commands.add_parser("down", help="remove shots containers, macvlan network and routing fixture")
    down.add_argument("--vm", action="store_true", help="also shut chr-lab down")
    down.set_defaults(run=cmd_down)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    args.run(args)


if __name__ == "__main__":
    main()
