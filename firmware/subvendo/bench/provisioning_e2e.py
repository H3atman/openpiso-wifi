#!/usr/bin/env python3
"""No devices or CHR: exercise flash.py against a fresh native Controller.

  .venv/bin/python -I bench/provisioning_e2e.py --binary ../../controller/bin/openpiso
"""
import argparse
import contextlib
import hashlib
import hmac
import http.cookiejar
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--binary", type=Path, required=True)
    ap.add_argument("--firmware", type=Path, default=ROOT / ".pio/build/provisioning/firmware.bin")
    ap.add_argument("--report", type=Path, default=ROOT / "bench/out/provisioning-e2e.json")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="openpiso-provisioning-e2e-") as work:
        work = Path(work)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        portal = "http://controller.example:8080"
        password = "local-provisioning-test-password"
        env = {**os.environ, "OPENPISO_ADMIN_PASSWORD": password}
        with (work / "controller.log").open("w+") as log:
            controller = subprocess.Popen(
                [str(args.binary.resolve()), "-listen", f"127.0.0.1:{port}",
                 "-data", str(work / "ledger"), "-portal-url", portal],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(origin + "/healthz", timeout=1):
                            break
                    except OSError:
                        if controller.poll() is not None:
                            raise AssertionError("Controller exited before becoming ready")
                        time.sleep(0.1)
                else:
                    raise AssertionError("Controller did not become ready")
                # Import the builder so the CLI's actual image can be checked in
                # memory, without returning the one-time secret through a file/pipe.
                spec = importlib.util.spec_from_file_location("flash", ROOT / "flash.py")
                flash = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(flash)
                built = []
                def build(credentials, mac):
                    result = config_image(credentials, mac)
                    built.append(result[0])
                    return result
                config_image = flash.config_image
                guarded = False
                def no_devices_or_files(event, args):
                    if not guarded:
                        return
                    if event == "subprocess.Popen":
                        raise RuntimeError("tool access forbidden")
                    if event == "open":
                        path, mode, flags = args
                        if isinstance(path, (str, bytes)) and os.fsdecode(path).startswith("/dev/"):
                            raise RuntimeError("device access forbidden")
                        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                            raise RuntimeError("file writes forbidden")
                sys.addaudithook(no_devices_or_files)
                firmware = work / "input.bin"
                firmware.write_bytes(args.firmware.read_bytes())
                command = [str(ROOT / "flash.py"), "--controller", origin,
                           "--mac", "a0:20:a6:11:0c:01", "--firmware", str(firmware), "--no-flash"]
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", command), patch.dict(os.environ, env), \
                     patch.object(flash, "config_image", build), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    guarded = True
                    try:
                        flash.main()
                    finally:
                        guarded = False
                config, = built
                assert not (work / "config.bin").exists(), "secret-bearing config image was stored on disk"
                assert len(config) == 4096 and config[312:] == b"\xff" * (4096 - 312)
                magic, vendo_id, secret, url, hostname, crc = struct.unpack("<II32s256s12sI", config[:312])
                assert magic == 0x3150434F and vendo_id == 1
                assert zlib.crc32(config[:308]) == crc
                assert url.rstrip(b"\0").decode() == portal
                assert hostname.rstrip(b"\0") == b"opnw-0C01"
                assert len(secret) == 32 and secret != bytes(32)
                assert flash.CONFIG_ADDRESS == 0x3FB000
                log.flush()
                output = (stdout.getvalue() + stderr.getvalue() + (work / "controller.log").read_text()).encode()
                assert secret not in output and secret.hex().encode() not in output and password.encode() not in output
                for path in work.rglob("*"):
                    # The Controller ledger deliberately persists its own key.
                    if path.is_file() and work / "ledger" not in path.parents:
                        data = path.read_bytes()
                        assert secret not in data and secret.hex().encode() not in data, "secret stored on disk"

                def report(key):
                    body = struct.pack(">B6I", 1, vendo_id, 1, 0, 0, 12345, 1)
                    frame = body + hmac.digest(key, body, "sha256")
                    req = urllib.request.Request(origin + "/vendo/messages", frame,
                                                 {"Content-Type": "application/octet-stream"})
                    try:
                        return urllib.request.urlopen(req).status
                    except urllib.error.HTTPError as error:
                        return error.code

                # Real Controller verification of the decoded key, not a DB side channel.
                assert report(secret) == 503, "valid signed first contact must reach Router-unavailable"
                assert report(bytes(32)) == 401
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                opener.open(origin + "/admin/login", urllib.parse.urlencode({"password": password}).encode()).close()
                with opener.open(origin + "/admin/state") as response:
                    state = json.load(response)
                vendo = state["vendos"][0]
                assert vendo["id"] == vendo_id and vendo["mac"] == "A0:20:A6:11:0C:01"
                assert vendo["status"] == "provisioned" and not vendo["ip"] and not vendo["hotspot_server"]
                assert secret.hex() not in json.dumps(state)
                duplicate = subprocess.run([sys.executable, "-I", *command], env=env,
                                           capture_output=True, text=True, timeout=30)
                assert duplicate.returncode != 0 and "409" in duplicate.stderr
                assert secret.hex() not in duplicate.stdout + duplicate.stderr
                artifact = {"passed": True, "hardware_access": False, "vendo_id": vendo_id,
                            "mac": vendo["mac"], "hostname": "opnw-0C01", "controller_url": portal,
                            "config_address": "0x3FB000", "config_crc_verified": True,
                            "config_in_memory_only": True, "file_writes_forbidden": True,
                            "signed_report_status": 503, "bad_hmac_status": 401,
                            "duplicate_status": 409,
                            "firmware_sha256": hashlib.sha256(firmware.read_bytes()).hexdigest()}
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(artifact, indent=2) + "\n")
                print(f"PASS: no-flash provisioning E2E; artifact {args.report}")
            finally:
                controller.terminate()
                controller.wait(timeout=10)


if __name__ == "__main__":
    main()
