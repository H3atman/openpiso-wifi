#!/usr/bin/env python3
"""A real Controller on an isolated Docker lab of its own (opnwlab-<6hex>), for running any command
against while it is up. Evidence goes to a run dir outside the repository, which outlives the lab.

Run: python3 -I .claude/skills/verify-controller/verify.py {doctor,up,exec <lab> -- cmd...,down <lab>}
"""

import argparse
import hashlib
import http.cookiejar
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "openpiso-verify"
BINARY = ROOT / "controller/bin/openpiso"
RATES = "1=60\n5=360"
DEVICE_MAC = "02:00:00:0E:00:01"  # locally administered, like every lab MAC
LAB_NAME = re.compile(r"opnwlab-[0-9a-f]{6}")


def load(path: str, name: str):
    # python -I omits the script directory from sys.path, so import by path (as lab/dev/dev.py does).
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e2e = load("controller/e2e/run.py", "e2e_run")  # router_rest, request, wait_for, ROUTER_ADMIN*
docker_lab = e2e._docker_module


def say(message: str) -> None:
    print(f"[verify] {message}", file=sys.stderr, flush=True)


def write_private(path: Path, text: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        file.write(text)


def go() -> str | None:
    found = shutil.which("go") or str(Path.home() / ".local/go/bin/go")
    return found if Path(found).exists() else None


def git_head() -> str:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def login(jar_path: Path, password: str) -> http.cookiejar.MozillaCookieJar:
    """Admin login; the session cookie is saved to jar_path in Netscape format, for `curl -b`."""
    jar = http.cookiejar.MozillaCookieJar(str(jar_path))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with e2e.request(opener, "/admin/login", "POST", {"password": password}) as response:
        assert response.status in (200, 303), f"admin login -> {response.status}"
    assert any(c.name == "openpiso_admin" for c in jar), "admin login set no openpiso_admin cookie"
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(jar_path, 0o600)
    jar.opener = opener
    return jar


def admin_post(opener, path: str, form: dict) -> None:
    with e2e.request(opener, path, "POST", form) as response:
        if response.status not in (200, 201, 204, 303):
            raise AssertionError(f"POST {path} -> {response.status}: {response.read().decode()[:300]}")


def admin_state(opener) -> dict:
    with e2e.request(opener, "/admin/state") as response:
        return json.load(response)


def start_controller(lab, password: str) -> None:
    """Start the Controller in the lab's controller container; its log is the container's (docker logs)."""
    container = f"{lab.name}-controller-1"
    script = ('echo $$ > /run/openpiso.pid; exec openpiso -listen 0.0.0.0:18080 -data /data -portal-url "$1" '
              '-admin-network "$(cat /run/admin-network)" >/proc/1/fd/1 2>&1')
    docker_lab.sh("docker", "exec", "-d", "-e", f"OPENPISO_ADMIN_PASSWORD={password}", container,
                  "sh", "-c", script, "sh", docker_lab.PORTAL_URL)
    e2e.wait_for(e2e.BASE_URL + "/healthz", timeout=30)


def cmd_up(_args) -> None:
    say("building the Controller (make -C controller build)")
    env = {**os.environ, "CGO_ENABLED": "0"}
    if gobin := go():
        env["PATH"] = f"{Path(gobin).parent}:{env['PATH']}"
    subprocess.run(["make", "-C", str(ROOT / "controller"), "build"], env=env, check=True,
                   stdout=sys.stderr)
    lab = docker_lab.Lab(BINARY, docker_lab.router_image(), docker_lab.lab_image())
    docker_lab.unwind_on_signals()  # a signal unwinds through the removal below
    try:
        say(f"creating lab {lab.name}")
        lab.up()
        router = docker_lab.DockerLab(e2e, BINARY, keep=True)
        router.attach(lab)
        router.set_up_router()
        router.new_router_certificate()  # also creates the bootstrap admin onboarding uses

        out = STATE / lab.name
        out.mkdir(mode=0o700, parents=True)
        password = secrets.token_urlsafe(16)
        write_private(out / "admin-password", password)
        e2e.BASE_URL = "http://" + lab.port("controller", 18080)
        say(f"starting the Controller at {e2e.BASE_URL}")
        start_controller(lab, password)
        opener = login(out / "cookies.txt", password).opener

        say("onboarding the Router")
        admin_post(opener, "/admin/router/probe", {"address": router.router_url, "username": e2e.ROUTER_ADMIN,
                                                   "password": e2e.ROUTER_ADMIN_PASSWORD})
        admin_post(opener, "/admin/router/confirm", {})
        # Use existing: Adopt the lab's hs1, which puts the Controller in the walled garden.
        admin_post(opener, "/admin/router/adopt", {"server": "hs1"})
        router_state = admin_state(opener).get("router") or {}
        if router_state.get("status") != "ready" or router_state.get("adopted_hotspot_servers") != ["hs1"]:
            raise AssertionError(f"Router is not ready with hs1 Adopted: {router_state}")
        admin_post(opener, "/admin/rates", {"tiers": RATES})

        device = lab.device("device", DEVICE_MAC)
        device_ip = docker_lab.device_lease(device)
        # The Router refuses the Device's first connections to the Portal for a moment after Adopt.
        docker_lab.device_exec(device, f"for i in $(seq 30); do curl -sf -m 2 -o /dev/null "
                                       f"{docker_lab.PORTAL_URL}/healthz && exit 0; sleep 1; done; exit 1")
        say(f"Device {device} leased {device_ip} and reaches the Portal")
        (out / "lab.json").write_text(json.dumps({
            "lab": lab.name, "controller_url": e2e.BASE_URL, "portal_url": docker_lab.PORTAL_URL,
            "router_rest": router.router_address, "router_rest_http": router._router_http,
            "device": device, "device_ip": device_ip, "device_mac": DEVICE_MAC,
            "binary": str(BINARY), "git_head": git_head()}, indent=2) + "\n")
    except BaseException:
        docker_lab.ignore_signals()
        say(f"up failed; removing lab {lab.name}")
        lab.down()
        raise
    say(f"lab {lab.name} is up; run dir {out}")
    print(lab.name)


def run_dir(name: str) -> Path:
    if not LAB_NAME.fullmatch(name):
        sys.exit(f"[verify] not a lab name: {name!r} (expected opnwlab-<6hex>)")
    return STATE / name


def cmd_exec(args) -> None:
    out = run_dir(args.lab)
    info = json.loads((out / "lab.json").read_text())
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        sys.exit("[verify] exec needs a command after --")
    env = {**os.environ, "OPNW_LAB": info["lab"], "OPNW_URL": info["controller_url"],
           "OPNW_JAR": str(out / "cookies.txt"), "OPNW_ADMIN_PASSWORD_FILE": str(out / "admin-password"),
           "OPNW_DEVICE": info["device"], "OPNW_DEVICE_IP": info["device_ip"], "OPNW_PORTAL": info["portal_url"],
           "OPNW_DEVICE_MAC": info["device_mac"], "OPNW_ROUTER": info["router_rest"],
           # The lab Router's admin password is a fixed lab constant (docker_lab.py), not a secret.
           "OPNW_ROUTER_PASSWORD": docker_lab.ROUTER_PASSWORD, "OPNW_OUT": str(out)}
    sys.exit(subprocess.run(command, env=env).returncode)


def cmd_down(args) -> None:
    out = run_dir(args.lab)
    lab = docker_lab.Lab.__new__(docker_lab.Lab)  # only its name is needed to find what to remove
    lab.name = args.lab
    lab.down()
    say(f"lab {args.lab} removed; run dir kept at {out}")


def cmd_doctor(_args) -> None:
    def check(ok: bool, what: str) -> bool:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
        return ok

    docker_ok = check(subprocess.run(["docker", "info"], capture_output=True).returncode == 0, "docker usable")
    check(Path("/dev/kvm").exists(), "/dev/kvm present")
    check(go() is not None, f"go found ({go()})")
    chr_image = docker_lab.CACHE / "chr" / f"chr-{docker_lab.CHR_VERSION}.img"
    check(chr_image.exists(), f"CHR image cached ({chr_image})")
    tag = "opnwlab-image:" + hashlib.sha256((docker_lab.LAB_DIR / "Dockerfile").read_bytes()).hexdigest()[:12]
    if docker_ok:
        check(subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0,
              f"lab image present ({tag}; `up` builds it otherwise)")
        names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
        labs = sorted({m.group(0) for m in map(LAB_NAME.match, names.split()) if m})
        for name in labs:
            print(f"lab  {name}" + ("  (run dir: " + str(STATE / name) + ")" if (STATE / name).exists() else ""))
        if not labs:
            print("lab  none running")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("up").set_defaults(func=cmd_up)
    run = sub.add_parser("exec")
    run.add_argument("lab")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_exec)
    down = sub.add_parser("down")
    down.add_argument("lab")
    down.set_defaults(func=cmd_down)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
