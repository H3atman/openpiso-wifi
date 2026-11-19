#!/usr/bin/env python3
"""Persistent local dev lab: a dev-build Controller, with browsers standing in for customer Devices.
It runs on a long-lived Docker lab of its own (opnwdev). See lab/dev/README.md.

Run: python3 -I lab/dev/dev.py {up,vendo,phone,shots,admin-shots,down}
"""

import argparse
import fcntl
import http.cookiejar
import importlib.util
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "lab/dev/.run"  # the admin password and shots output; they outlive the lab
RATES = "1=60\n5=360"  # seeded only when the Controller has no rate tiers
VENDO_MAC = "02:00:00:0D:00:F0"  # locally administered, like every lab MAC
PHONE_MAC = "02:00:00:0d:00:03"  # `phone`

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


# One long-lived lab, the E2E lab's compose project (lab/docker) under a fixed name plus
# lab/docker/dev.yml. E2E labs are opnwlab-<random>, so they run beside it.
DOCKER_LAB = "opnwdev"
ADMIN_PORT = 18088  # the admin panel's fixed local port
CONTROLLER = f"http://127.0.0.1:{ADMIN_PORT}"  # admin and Vendo traffic, from the host
STATE = RUN / "docker"  # dev binary and Vendo state; they go with the lab, the admin password stays
DOCKER_CONTROLLER = f"{DOCKER_LAB}-controller-1"
DOCKER_VENDO = f"{DOCKER_LAB}-vendo"  # the dev Vendo board, sending every Report from its own address
VENDO_SENDERS = {}  # Vendo ID -> its board container (docker_lab.container_vendo)
DOCKER_PHONE = f"{DOCKER_LAB}-phone"  # `phone`: a proxy on the lab LAN as one Device, for your own browser
PHONE_PORT = 18089  # the proxy's fixed local port

e2e = load("controller/e2e/run.py", "e2e_run")  # router_rest, wait_for, request, ROUTER_ADMIN*
e2e.BASE_URL = CONTROLLER  # e2e.request and e2e.wait_for address the dev Controller
router_rest = e2e.router_rest
docker_lab = e2e._docker_module
SimulatedVendo = docker_lab.container_vendo(e2e.SimulatedVendo, VENDO_SENDERS)


def say(message: str) -> None:
    print(f"[dev] {message}", flush=True)


def die(message: str) -> None:
    sys.exit(f"[dev] {message}")


def docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    if check and result.returncode != 0:
        die(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def forget_device(address: str, password: str, mac: str, ip: str) -> None:
    """Remove a Device's HotSpot host, session and static lease. A stale host makes its IP ambiguous."""
    mac = mac.upper()  # RouterOS queries are case-sensitive
    for path, query in (("/ip/hotspot/host", f"address={ip}"), ("/ip/hotspot/host", f"mac-address={mac}"),
                        ("/ip/hotspot/active", f"mac-address={mac}"), ("/ip/dhcp-server/lease", f"mac-address={mac}")):
        for entry in router_rest(address, "admin", password, f"{path}?{query}"):
            router_rest(address, "admin", password, f"{path}/{entry['.id']}", "DELETE")


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


def onboard_router(admin: Admin, address: str, password: str, url: str) -> None:
    """Pin the Router's current certificate, as the operator does on the Router page. address is where
    this script reaches the Router's REST; url is where the Controller does.

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
        admin.post("/admin/router/probe", {"address": url, "username": e2e.ROUTER_ADMIN,
                                           "password": e2e.ROUTER_ADMIN_PASSWORD})
        admin.post("/admin/router/confirm", {})
    finally:
        drop_user(e2e.ROUTER_ADMIN)


def ensure_vendo(admin: Admin, address: str, password: str) -> dict:
    """Exactly one live dev Vendo; its id/secret/epoch live in .run/vendo.json. Pairing only provisions
    it (flash-time pairing): it stays provisioned or pending until adopt_vendo's first Report."""
    path = STATE / "vendo.json"
    saved = json.loads(path.read_text()) if path.exists() else None
    live = [v for v in admin.state().get("vendos") or [] if v["status"] != "unpaired"]
    if saved and [v["id"] for v in live] == [saved["id"]]:
        return saved
    for vendo in live:  # secret lost, or another Vendo: start clean
        admin.post("/admin/vendos/unpair", {"id": vendo["id"]})
    e2e.cleanup_vendo_router(address, password)  # stale openpiso-vendo-* rules from other runs
    result = json.loads(admin.post("/admin/vendos/pair", {"mac": VENDO_MAC}))
    saved = {"id": result["id"], "secret": result["secret"], "mac": VENDO_MAC, "epoch": saved["epoch"] if saved else 0}
    write_private(path, json.dumps(saved))
    say(f"provisioned dev Vendo {saved['id']} ({VENDO_MAC})")
    return saved


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)


def build_controller(binary: Path) -> Path:
    go = shutil.which("go") or str(Path.home() / ".local/go/bin/go")
    say("building the Controller (-tags dev)")
    result = subprocess.run([go, "build", "-tags", "dev", "-o", str(binary), "./cmd/openpiso"],
                            cwd=ROOT / "controller", env={**os.environ, "CGO_ENABLED": "0"})
    if result.returncode != 0:
        die("go build failed")
    return binary


@contextmanager
def vendo_boot():
    """Take the Vendo pidfile lock and bump the persisted epoch: each holder is one board boot."""
    path = STATE / "vendo.json"
    if not path.exists():
        die("no dev Vendo yet: run `up` first")
    lock = open(STATE / "vendo.pid", "a+")
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


def ensure_image() -> None:
    if docker("image", "inspect", PLAYWRIGHT_IMAGE, check=False).returncode != 0:
        say(f"building {PLAYWRIGHT_IMAGE} (one time)")
        dockerfile = f"FROM {PLAYWRIGHT_BASE}\nRUN pip install --no-cache-dir --break-system-packages playwright==1.64.1\n"
        subprocess.run(["docker", "build", "-q", "-t", PLAYWRIGHT_IMAGE, "-"], input=dockerfile, text=True, check=True)


def record_shot(report: dict, name: str, mac: str, ip: str, started: float, profile_dir: Path, reason: str,
                stdout: str, stderr: str) -> None:
    """Add one profile's result.json (or the failure reason) to report and print its verdict."""
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


class DevLab(docker_lab.Lab):
    """The dev lab: the E2E lab's compose project under DOCKER_LAB, with dev.yml mounting the repository
    at its real path (the dev build reads web/ from there) and publishing the admin on ADMIN_PORT."""

    def __init__(self):
        super().__init__(STATE / "openpiso-dev", docker_lab.router_image(), docker_lab.lab_image())
        self.name = DOCKER_LAB
        self.env.update(LAB=DOCKER_LAB, SOURCE=str(ROOT), ADMIN_PORT=str(ADMIN_PORT))

    def compose(self, *args: str) -> str:
        files = [arg for name in ("compose.yml", "dev.yml") for arg in ("-f", str(docker_lab.LAB_DIR / name))]
        return docker_lab.sh("docker", "compose", *files, *args, env={**os.environ, **self.env})


def running(container: str) -> bool:
    return docker("inspect", "-f", "{{.State.Running}}", container, check=False).stdout.strip() == "true"


def start_docker_controller(binary: Path, admin_password: str) -> None:
    """(Re)start the dev Controller in its container, from the binary in the mounted repository. Its
    data is the container's /data, so it survives restarts and goes with the lab. Its log is the
    container's (docker logs)."""
    docker("exec", DOCKER_CONTROLLER, "sh", "-c", "pid=$(cat /run/openpiso.pid 2>/dev/null) || exit 0; "
           "kill $pid 2>/dev/null; while kill -0 $pid 2>/dev/null; do sleep 0.1; done")
    script = (f'echo $$ > /run/openpiso.pid; exec {binary} -listen 0.0.0.0:18080 -data /data -portal-url "$1" '
              '-admin-network "$(cat /run/admin-network)" >/proc/1/fd/1 2>&1')
    docker("exec", "-d", "-e", f"OPENPISO_ADMIN_PASSWORD={admin_password}", DOCKER_CONTROLLER,
           "sh", "-c", script, "sh", docker_lab.PORTAL_URL)
    e2e.wait_for(CONTROLLER + "/healthz", timeout=30)
    if docker("exec", DOCKER_CONTROLLER, "sh", "-c", "kill -0 $(cat /run/openpiso.pid)", check=False).returncode:
        die(f"the Controller exited after starting: docker logs {DOCKER_CONTROLLER}")


def cmd_up(_args) -> None:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary = build_controller(STATE / "openpiso-dev")
    lab = DevLab()
    router = docker_lab.DockerLab(e2e, binary, keep=True)
    if not lab.containers():
        try:
            urllib.request.urlopen(CONTROLLER + "/healthz", timeout=1)
            die(f"something already answers on 127.0.0.1:{ADMIN_PORT}; stop it first")
        except OSError:
            pass
        (STATE / "vendo.json").unlink(missing_ok=True)  # paired with an earlier lab's Controller
        say(f"creating the dev lab {DOCKER_LAB} (stock CHR {docker_lab.CHR_VERSION})")
        lab.up()
        router.attach(lab)
        router.set_up_router()
        router.new_router_certificate()
    elif not all(running(f"{DOCKER_LAB}-{service}-1") for service in ("router", "controller", "internet")):
        die(f"the dev lab {DOCKER_LAB} is not fully running (after a reboot?): run `down`, then `up`")
    else:
        router.attach(lab)
        say(f"dev lab {DOCKER_LAB} is up; restarting its Controller")

    admin_file = RUN / "admin-password"
    if not admin_file.exists():
        write_private(admin_file, secrets.token_urlsafe(12))
    admin_password = admin_file.read_text().strip()
    start_docker_controller(binary, admin_password)
    admin = Admin(admin_password)
    if (admin.state().get("router") or {}).get("status") != "ready":
        say("onboarding the Router")
        onboard_router(admin, router.router_address, docker_lab.ROUTER_PASSWORD, router.router_url)
        if (status := admin.state().get("router") or {}).get("status") != "ready":
            die(f"Router is not ready after onboarding: {status}")
    VENDO_SENDERS[ensure_vendo(admin, router.router_address, docker_lab.ROUTER_PASSWORD)["id"]] = DOCKER_VENDO
    adopt_vendo(admin, lab)
    if not admin.state().get("rate_tiers"):
        admin.post("/admin/rates", {"tiers": RATES})
        say(f"seeded Coin rates: {RATES.replace(chr(10), ', ')}")
    print(f"""
Dev lab {DOCKER_LAB} is up; it stays up until `down`.
  Admin:     {CONTROLLER}/admin/login  (password in {admin_file})
  Portal:    {docker_lab.PORTAL_URL}/portal  (only reachable by Devices behind the HotSpot)
  Log:       docker logs -f {DOCKER_CONTROLLER}
  Next:      python3 -I lab/dev/dev.py vendo   # coin slot for the dev Vendo
             python3 -I lab/dev/dev.py phone   # a Device for your own browser (proxy)
             python3 -I lab/dev/dev.py shots   # scripted phones, screenshots
             python3 -I lab/dev/dev.py down    # remove the lab and its data
Template/CSS/JS edits show on refresh. After Go changes run `up` again (it rebuilds; data is kept).
""", flush=True)


def adopt_vendo(admin: Admin, lab: DevLab) -> None:
    """Finish the dev Vendo's pairing: its board is a container on the HotSpot LAN with its own MAC, lease
    and opnw-XXXX hostname, and sends its first Report (and every later one) from there."""
    with vendo_boot() as state:
        status = lambda: next(v["status"] for v in admin.state()["vendos"] if v["id"] == state["id"])
        if status() == "paired":
            return
        if DOCKER_VENDO not in lab.containers():
            lab.device("vendo", VENDO_MAC)
            docker_lab.device_lease(DOCKER_VENDO, hostname=docker_lab.vendo_hostname(VENDO_MAC))
        say(f"pairing dev Vendo {state['id']}: first Report from {DOCKER_VENDO}")
        board = SimulatedVendo(CONTROLLER, state["id"], bytes.fromhex(state["secret"]), state["epoch"])
        deadline = time.monotonic() + 40
        while status() != "paired":  # pairing resets the first connection, so Report until it shows
            if time.monotonic() > deadline:
                die(f"dev Vendo {state['id']} is {status()} after 40 s of Reports")
            try:
                board.post(board.frame())
            except OSError:
                pass
            time.sleep(1)
        say(f"paired dev Vendo {state['id']}")


def cmd_shots(args) -> None:
    for name in args.profile:
        if name not in PROFILES:
            die(f"unknown profile {name}; known: {', '.join(PROFILES)}")
    if not running(DOCKER_CONTROLLER):
        die(f"no dev lab {DOCKER_LAB}: run `up` first")
    lab = DevLab()
    router = docker_lab.DockerLab(e2e, STATE / "openpiso-dev", keep=True)
    router.attach(lab)
    address, password = router.router_address, docker_lab.ROUTER_PASSWORD
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUN / "shots" / stamp
    report = {"started": stamp, "profiles": []}
    ensure_image()
    with vendo_boot() as state:
        release_windows(state)
        try:
            for name in args.profile:
                mac = "02:de:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(4))
                container, ip = f"{DOCKER_LAB}-shots-{name}", ""
                profile_dir = out / name
                profile_dir.mkdir(parents=True)
                state["epoch"] += 1  # each phone's in-process Vendo is one board boot
                write_private(STATE / "vendo.json", json.dumps(state))
                write_private(out / "vendo.json", json.dumps(state))
                started = time.monotonic()
                stdout = stderr = ""
                try:
                    # Started idle, so it can take its lease from the Router before the journey begins.
                    docker("run", "-d", "--init", "--ipc=host", "--name", container, "--network", f"name={DOCKER_LAB}-lan",
                           "--mac-address", mac, *docker_lab.DockerLab.browser_limits,
                           "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                           "-v", f"{ROOT / 'lab/dev/shots.py'}:/work/shots.py:ro",
                           "-v", f"{ROOT / 'controller/e2e/simulated_vendo.py'}:/work/simulated_vendo.py:ro",
                           "-v", f"{out / 'vendo.json'}:/work/vendo.json:ro",
                           "-v", f"{profile_dir}:/out", PLAYWRIGHT_IMAGE, "sleep", "infinity")
                    ip = docker_lab.lease_for(lab, container)[0]
                    say(f"{name}: {PROFILES[name]} as {mac} {ip}")
                    run = subprocess.run(
                        ["docker", "exec", container, "python", "/work/shots.py", "--device", PROFILES[name],
                         "--controller", docker_lab.PORTAL_URL, "--vendo", "/work/vendo.json", "--out", "/out",
                         "--start", "http://google.com/", "--online-text", docker_lab.INTERNET_BODY],
                        capture_output=True, text=True, timeout=300)
                    stdout, stderr = run.stdout, run.stderr
                    reason = "flow did not finish"
                except subprocess.TimeoutExpired:
                    reason = "timed out after 300 s"
                finally:
                    docker("rm", "-f", container, check=False)
                    if ip:
                        forget_device(address, password, mac, ip)
                record_shot(report, name, mac, ip, started, profile_dir, reason, stdout, stderr)
        finally:
            (out / "vendo.json").unlink(missing_ok=True)  # holds the Vendo secret
            write_private(out / "report.json", json.dumps(report, indent=2))
    if any(p["result"] != "pass" for p in report["profiles"]):
        sys.exit(1)


def write_phone_profile() -> Path:
    """A fresh Firefox profile for `phone`, made again on every start so no cookie outlives the Device:
    every request through the proxy, DoH off, and captive-portal detection checking the internet
    stand-in's page (its stock URL does not resolve in the sealed lab)."""
    profile = STATE / "firefox-profile"
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True)
    prefs = {"network.proxy.type": 1, "network.proxy.http": "127.0.0.1", "network.proxy.http_port": PHONE_PORT,
             "network.proxy.ssl": "127.0.0.1", "network.proxy.ssl_port": PHONE_PORT, "network.trr.mode": 5,
             "captivedetect.canonicalURL": "http://google.com/",
             "captivedetect.canonicalContent": docker_lab.INTERNET_BODY + "\n",
             "browser.shell.checkDefaultBrowser": False, "browser.aboutwelcome.enabled": False,
             "datareporting.policy.dataSubmissionEnabled": False}
    (profile / "user.js").write_text("".join(f"user_pref({json.dumps(name)}, {json.dumps(value)});\n"
                                             for name, value in prefs.items()))
    return profile


def cmd_phone(_args) -> None:
    """A Device for your own browser: a container on the lab LAN with PHONE_MAC and a lease from the
    Router, running lab/dev/phone_proxy.py, published on 127.0.0.1:PHONE_PORT. The lab LAN is internal
    (no published ports), so the container also joins a network of its own that only carries the
    published port; its default route goes, as the Controller's does, and replies to the host stay
    on-link. Runs until Ctrl+C, then the container goes and the Router forgets the Device."""
    if not running(DOCKER_CONTROLLER):
        die(f"no dev lab {DOCKER_LAB}: run `up` first")
    if DOCKER_PHONE in DevLab().containers():
        die(f"{DOCKER_PHONE} already exists: another `phone` runs (or remove it with docker rm -f {DOCKER_PHONE})")
    try:
        socket.create_connection(("127.0.0.1", PHONE_PORT), timeout=1).close()
        die(f"something already listens on 127.0.0.1:{PHONE_PORT}; stop it first")
    except OSError:
        pass
    lab = DevLab()
    router = docker_lab.DockerLab(e2e, STATE / "openpiso-dev", keep=True)
    router.attach(lab)
    address, password = router.router_address, docker_lab.ROUTER_PASSWORD
    port_network = f"{DOCKER_LAB}-phone-port"
    if docker("network", "inspect", port_network, check=False).returncode != 0:
        docker("network", "create", port_network)
    ifname = "driver-opt=com.docker.network.endpoint.ifname"
    docker("run", "-d", "--name", DOCKER_PHONE, "--cap-add", "NET_ADMIN",
           "--network", f"name={port_network},{ifname}=port0",
           "--network", f"name={DOCKER_LAB}-lan,mac-address={PHONE_MAC},{ifname}=eth0",
           "-p", f"127.0.0.1:{PHONE_PORT}:8888", "--memory", "128m", "--memory-swap", "128m",
           "-v", f"{ROOT / 'lab/dev/phone_proxy.py'}:/work/phone_proxy.py:ro",
           lab.image, "python3", "-I", "/work/phone_proxy.py", "8888")
    ip = ""
    try:
        docker_lab.device_exec(DOCKER_PHONE, "ip route del default; ip -4 addr flush dev eth0; ip -4 route flush dev eth0")
        ip = docker_lab.device_lease(DOCKER_PHONE)
        lease = router_rest(address, "admin", password, f"/ip/dhcp-server/lease?mac-address={PHONE_MAC.upper()}")
        say(f"phone {DOCKER_PHONE} is a Device on the lab LAN: {PHONE_MAC.upper()} {ip}, "
            f"Router lease {[row['address'] for row in lease]}")
        profile = write_phone_profile()
        print(f"""
Proxy: http://127.0.0.1:{PHONE_PORT}  (your browser is the Device {PHONE_MAC.upper()} {ip} while this runs)
Open a browser on it with a fresh profile, in another terminal:
  firefox --no-remote --new-instance --profile {profile} http://google.com/
  chromium --user-data-dir="$(mktemp -d)" --proxy-server=http://127.0.0.1:{PHONE_PORT} http://google.com/
Coins: python3 -I lab/dev/dev.py vendo   # start it before "Insert coin", then type pulses
Ctrl+C here removes the phone; the Router forgets the Device, so the next `phone` starts without Time Credit.
""", flush=True)
        while running(DOCKER_PHONE):
            time.sleep(2)
        say(f"{DOCKER_PHONE} is gone")
    except KeyboardInterrupt:
        pass
    finally:
        docker("rm", "-f", DOCKER_PHONE, check=False)
        if ip:
            forget_device(address, password, PHONE_MAC, ip)
        say("phone stopped; the Router forgot its Device")


def cmd_down(_args) -> None:
    DevLab().down()
    (STATE / "vendo.json").unlink(missing_ok=True)
    say(f"down: dev lab {DOCKER_LAB} removed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="start the dev Controller and make the lab healthy").set_defaults(run=cmd_up)
    commands.add_parser("vendo", help="interactive coin slot for the dev Vendo").set_defaults(run=cmd_vendo)
    commands.add_parser("phone", help=f"a proxy Device on 127.0.0.1:{PHONE_PORT} for your own browser").set_defaults(run=cmd_phone)
    shots = commands.add_parser("shots", help="scripted phones through the HotSpot, with screenshots")
    shots.add_argument("profile", nargs="*", default=list(PROFILES), help=f"default: {' '.join(PROFILES)}")
    shots.set_defaults(run=cmd_shots)
    admin_shots = commands.add_parser("admin-shots", help="admin panel screenshots from fixture data (no Router)")
    admin_shots.add_argument("--out", required=True, help="directory for the PNGs")
    admin_shots.set_defaults(run=cmd_admin_shots)
    commands.add_parser("down", help=f"remove the dev lab {DOCKER_LAB} and its data").set_defaults(run=cmd_down)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if (STATE / "vendo.json").exists():
        VENDO_SENDERS[json.loads((STATE / "vendo.json").read_text())["id"]] = DOCKER_VENDO
    args.run(args)


if __name__ == "__main__":
    main()
