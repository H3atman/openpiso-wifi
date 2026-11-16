#!/usr/bin/env python3
"""Provision a Vendo and write its firmware plus credentials. Run with .venv/bin/python.

No-flash mode requires --mac, reports only non-secret fields and never invokes
esptool. Firmware and credentials are passed to esptool in memory, never saved.
"""
import argparse
import base64
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

CONFIG_ADDRESS = 0x3FB000
CONFIG_MAGIC = 0x3150434F
ROOT = Path(__file__).resolve().parent


class ProvisionError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the admin credential, or provision at a different origin.
        return None


def http_url(value, *, management=False):
    try:
        url = urllib.parse.urlsplit(value)
        value.encode("ascii")
        valid = (url.scheme in (("http", "https") if management else ("http",)) and url.hostname and not url.username and not url.password
                 and not url.query and not url.fragment and not any(ord(c) <= 32 or ord(c) >= 127 for c in value))
        if not valid or (url.port is not None and not 0 < url.port <= 65535):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ProvisionError("unsupported URL: use ASCII HTTP (or HTTPS for management), without credentials, query or fragment") from None
    return value.rstrip("/")


def normalize_mac(value):
    if not re.fullmatch(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}", value):
        raise ProvisionError("MAC must contain six colon-separated hex bytes")
    mac = bytes.fromhex(value.replace(":", ""))
    if mac[0] & 1 or not any(mac):
        raise ProvisionError("MAC must be nonzero and unicast")
    return value.upper()


def esptool(port, baud, *operation):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "esptool", "--chip", "esp8266", "--port", port,
         "--baud", str(baud), *operation], capture_output=True, text=True, timeout=180,
    )
    if result.returncode:
        raise ProvisionError("esptool failed; no tool output is logged (it may contain credentials)")
    return result.stdout


def write_images(port, baud, firmware, image):
    # esptool 5.6.0 accepts bytes in addr_data; do not create credential files.
    # Suppress third-party output and exceptions, which may contain image data.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            import esptool as tool
            esp = tool.detect_chip(port, baud)
            try:
                if esp.CHIP_NAME != "ESP8266":
                    raise ProvisionError("expected an ESP8266")
                esp = tool.run_stub(esp)
                tool.attach_flash(esp)
                tool.write_flash(esp, [(0, firmware), (CONFIG_ADDRESS, image)],
                                 flash_mode="dio", flash_size="4MB")
                tool.reset_chip(esp)
            finally:
                esp._port.close()
        except Exception:
            raise ProvisionError("esptool failed; no tool output is logged (it may contain credentials)") from None


def pair(origin, mac, password):
    authorization = base64.b64encode(("admin:" + password).encode()).decode()
    request = urllib.request.Request(
        origin + "/admin/vendos/pair", urllib.parse.urlencode({"mac": mac}).encode(),
        {"Authorization": "Basic " + authorization, "Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 201:
                raise ProvisionError("Controller did not return 201")
            credentials = json.loads(response.read(4096))
    except urllib.error.HTTPError as error:
        # Do not echo a response body: the successful response is a credential delivery.
        raise ProvisionError(f"Controller pair failed (HTTP {error.code})") from None
    except (OSError, ValueError):
        raise ProvisionError("Controller pair request failed or returned invalid JSON") from None
    return credentials


def config_image(credentials, mac):
    try:
        vendo_id = credentials["id"]
        secret_hex = credentials["secret"]
        if type(vendo_id) is not int or not 0 < vendo_id <= 0xFFFFFFFF:
            raise ValueError
        if not isinstance(secret_hex, str) or not re.fullmatch(r"[0-9a-f]{64}", secret_hex):
            raise ValueError
        url = http_url(credentials["controller_url"]).encode("ascii")
        if len(url) > 255:
            raise ValueError
        hostname = "opnw-" + mac.replace(":", "")[-4:]
        body = struct.pack("<II32s256s12s", CONFIG_MAGIC, vendo_id, bytes.fromhex(secret_hex),
                           url, hostname.encode("ascii"))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProvisionError("Controller returned invalid provisioning fields") from None
    record = body + struct.pack("<I", zlib.crc32(body))
    return record + b"\xff" * (4096 - len(record)), vendo_id, hostname


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--controller", required=True, help="management origin (trusted network; HTTP Basic)")
    ap.add_argument("--port", help="serial port; required for real flashing")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--mac", help="MAC override; required in no-flash mode")
    ap.add_argument("--firmware", type=Path, default=ROOT / ".pio/build/provisioning/firmware.bin")
    ap.add_argument("--no-flash", action="store_true", help="report non-secret config fields, access no device")
    args = ap.parse_args()
    if args.no_flash:
        if not args.mac or args.port:
            ap.error("--no-flash requires --mac and forbids --port")
    elif not args.port:
        ap.error("flashing requires --port")
    origin = http_url(args.controller, management=True)
    if urllib.parse.urlsplit(origin).path:
        raise ProvisionError("--controller must be a management origin, without a path")
    firmware = args.firmware.read_bytes()
    if not firmware or firmware[0] != 0xE9 or len(firmware) > 0xFEFF0:
        raise ProvisionError("expected an ESP8266 firmware image fitting the 4m1m sketch region")
    password = os.environ.pop("OPENPISO_ADMIN_PASSWORD", None)
    if not password:
        password = getpass.getpass("Controller admin password: ")
    if args.mac:
        mac = normalize_mac(args.mac)
    else:
        output = esptool(args.port, args.baud, "read-mac")
        match = re.search(r"MAC:\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", output)
        if not match:
            raise ProvisionError("esptool did not return a MAC")
        mac = normalize_mac(match[1])
    provisioned = False
    try:
        credentials = pair(origin, mac, password)
        provisioned = True
        image, vendo_id, hostname = config_image(credentials, mac)
        if args.no_flash:
            crc = struct.unpack_from("<I", image, 308)[0]
            print(f"Provisioned Vendo {vendo_id} ({mac}, {hostname}); "
                  f"URL={credentials['controller_url']} CRC={crc:08X}; NOTHING flashed or saved")
        else:
            write_images(args.port, args.baud, firmware, image)
            print(f"Flashed Vendo {vendo_id} ({mac}, {hostname}); reset without the GPIO0 strap")
    except BaseException:
        if provisioned:
            print("Credentials were issued. If nothing was flashed or flashing failed, unpair in admin before retrying.",
                  file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        main()
    except (ProvisionError, OSError, subprocess.SubprocessError):
        # Exception text from third-party libraries can include requests or data. Only our
        # own errors are safe to display; never print a traceback with credential locals.
        error = sys.exception()
        print(str(error) if isinstance(error, ProvisionError) else "Provisioning failed (I/O or tool error)",
              file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Provisioning interrupted", file=sys.stderr)
        sys.exit(130)
