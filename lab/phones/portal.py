"""Phone captive-portal test server (Q3 lab).

Usage:
    CHR_PASSWORD=... CHR_WAN_IP=... python3 -I lab/phones/portal.py \
        [--bind 192.168.122.1] [--port 8080] --out RUN_DIR

The CHR HotSpot login.html redirects phones to GET /?mac=..&ip=..  The portal
identifies a device by an `opid` cookie, maps it to a HotSpot user `p-<opid[:8]>`
(RUN_DIR/identities.json), and logs in / rebinds that user over the CHR REST API.
Every request is appended as one JSON line to RUN_DIR/portal.jsonl.
Stdlib only, single-threaded.
"""

import argparse
import base64
import html
import json
import os
import re
import secrets
import sys
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

COOKIE_MAX_AGE = 30 * 24 * 3600
OPID_RE = re.compile(r"^[0-9a-f]{12}$")

WAN_IP = os.environ.get("CHR_WAN_IP", "")
AUTH = "Basic " + base64.b64encode(f"admin:{os.environ.get('CHR_PASSWORD', '')}".encode()).decode()

OUT_DIR = ""
identities: dict[str, str] = {}  # opid -> hotspot user


def rest(method: str, path: str, body: dict | None = None) -> tuple[bool, object]:
    """Call the CHR REST API. Returns (ok, parsed JSON or error detail); never raises."""
    req = Request(
        f"http://{WAN_IP}/rest{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return True, (json.loads(raw) if raw else None)
    except HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw)
            return False, err.get("detail") or err.get("message") or str(err)
        except ValueError:
            return False, f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return False, str(e)


def ensure_user(user: str) -> str:
    """Create the HotSpot user (password = name). Returns 'ok' or 'error: ...'."""
    ok, data = rest("PUT", "/ip/hotspot/user", {
        "name": user, "password": user, "profile": "phone", "limit-uptime": "1h",
    })
    if ok or "already" in str(data):
        return "ok"
    return f"error: {data}"


def active_for(user: str) -> tuple[bool, list[dict]]:
    ok, data = rest("GET", f"/ip/hotspot/active?user={user}")
    return ok, (data if ok and isinstance(data, list) else [])


def hotspot_login(user: str, mac: str, ip: str) -> str:
    ok, data = rest("POST", "/ip/hotspot/active/login", {
        "user": user, "password": user, "mac-address": mac, "ip": ip,
    })
    return "ok" if ok else f"error: {data}"


def ua_class(ua: str) -> str:
    # Heuristic only: iOS CNA sends an iPhone/iPad UA without "Safari"; Android's
    # CaptivePortalLogin is a WebView, usually marked " wv)" in the UA.
    if "iPhone" in ua or "iPad" in ua:
        return "ios-safari" if "Safari" in ua else "ios-cna?"
    if "Android" in ua:
        return "android-webview?" if " wv)" in ua else "android-chrome"
    return "other"


def save_identities() -> None:
    with open(os.path.join(OUT_DIR, "identities.json"), "w") as f:
        json.dump(identities, f, indent=2)


def page(title: str, *lines: str) -> bytes:
    body = "".join(f"<p>{l}</p>" for l in lines)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:18px sans-serif;margin:1.5em}button{font-size:1.2em;padding:.6em 1.2em}"
        "code{background:#eee;padding:0 .2em}</style></head>"
        f"<body><h2>{html.escape(title)}</h2>{body}</body></html>"
    ).encode()


def esc(v: object) -> str:
    return html.escape(str(v))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:  # the JSONL is the log
        pass

    # -- plumbing ----------------------------------------------------------

    def _cookie_opid(self) -> str | None:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        val = jar["opid"].value if "opid" in jar else ""
        return val if OPID_RE.match(val) else None

    def _reply(self, body: bytes, set_opid: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_opid:
            self.send_header(
                "Set-Cookie", f"opid={set_opid}; Max-Age={COOKIE_MAX_AGE}; Path=/; SameSite=Lax"
            )
        self.end_headers()
        self.wfile.write(body)

    def _log(self, event: str, fields: dict) -> None:
        ua = self.headers.get("User-Agent", "")
        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "method": self.command,
            "path": self.path,
            "remote": self.client_address[0],
            "mac": None,
            "ip": None,
            "ua": ua,
            "ua_class": ua_class(ua),
            "cookie_opid": self._cookie_opid(),
        }
        rec.update(fields)
        with open(os.path.join(OUT_DIR, "portal.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/":
            return self._other()
        q = parse_qs(url.query)
        mac = q.get("mac", [""])[0]
        ip = q.get("ip", [""])[0]
        cookie = self._cookie_opid()

        if not mac:
            return self._browser_check(cookie)
        if cookie and cookie in identities:
            return self._returning(cookie, mac, ip)
        self._new_device(mac, ip)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/connect":
            return self._other()
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        mac = form.get("mac", [""])[0]
        ip = form.get("ip", [""])[0]
        cookie = self._cookie_opid()
        user = identities.get(cookie) if cookie else None
        if not user:
            result = "error: no known opid cookie sent with the Connect request"
            self._log("login", {"mac": mac, "ip": ip, "opid": cookie, "user": None, "result": result})
            return self._reply(page("Not connected", "The identity cookie did not come back with this request.", esc(result)))
        result = hotspot_login(user, mac, ip)
        self._log("login", {"mac": mac, "ip": ip, "opid": cookie, "user": user, "result": result})
        if result == "ok":
            self._reply(page("Connected", "You can close this."))
        else:
            self._reply(page("Login failed", esc(result)))

    def _other(self) -> None:
        self._log("other", {"result": "ok"})
        self._reply(page("openpiso phone portal", "Nothing here."))

    def _browser_check(self, cookie: str | None) -> None:
        user = identities.get(cookie) if cookie else None
        if user:
            lines = [f"Cookie <code>opid</code> present: <code>{esc(cookie)}</code>", f"It maps to HotSpot user <code>{esc(user)}</code>."]
            result = "cookie-known"
        elif cookie:
            lines = [f"Cookie <code>opid</code> present: <code>{esc(cookie)}</code>", "But this run does not know it."]
            result = "cookie-unknown"
        else:
            lines = ["No <code>opid</code> cookie was sent to this browser."]
            result = "no-cookie"
        self._log("visit", {"action": "browser-check", "opid": cookie, "user": user, "result": result})
        self._reply(page("Cookie check", *lines))

    def _new_device(self, mac: str, ip: str) -> None:
        opid = secrets.token_hex(6)
        user = f"p-{opid[:8]}"
        result = ensure_user(user)
        if result == "ok":
            identities[opid] = user
            save_identities()
        self._log("visit", {
            "action": "new", "mac": mac, "ip": ip, "opid": opid, "user": user, "result": result,
        })
        lines = [f"MAC <code>{esc(mac)}</code>, IP <code>{esc(ip)}</code>."]
        if result != "ok":
            lines.append(f"Could not create the user: {esc(result)}")
        form = (
            '<form method="post" action="/connect">'
            f'<input type="hidden" name="mac" value="{esc(mac)}">'
            f'<input type="hidden" name="ip" value="{esc(ip)}">'
            "<button>Connect</button></form>"
        )
        self._reply(page("New device", *lines, form), set_opid=opid)

    def _returning(self, opid: str, mac: str, ip: str) -> None:
        user = identities[opid]
        base = {"mac": mac, "ip": ip, "opid": opid, "user": user}
        ok, entries = active_for(user)
        if not ok:
            self._log("visit", {**base, "action": "error", "result": "error: could not read active list"})
            return self._reply(page("Router error", "Could not read the active sessions from the router."))
        if any(e.get("mac-address", "").lower() == mac.lower() for e in entries):
            self._log("visit", {**base, "action": "already", "result": "ok"})
            return self._reply(page("Already connected", f"This device ({esc(mac)}) already has a session."))

        # Remove first: with shared-users=1 a login while the old entry exists fails.
        old_mac = entries[0].get("mac-address") if entries else None
        for e in entries:
            rest("DELETE", f"/ip/hotspot/active/{e['.id']}")
        result = hotspot_login(user, mac, ip)
        action = "rebind" if entries else "auto-login"
        self._log("visit", {**base, "action": action, "old_mac": old_mac, "result": result})
        if result != "ok":
            return self._reply(page("Login failed", esc(result)))
        lines = ["Reconnected automatically."]
        if old_mac and old_mac.lower() != mac.lower():
            lines.append(f"Rebound from <code>{esc(old_mac)}</code> to <code>{esc(mac)}</code>.")
        self._reply(page("Reconnected", *lines))


def main() -> None:
    global OUT_DIR
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="192.168.122.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--out", required=True, help="run directory for portal.jsonl and identities.json")
    args = ap.parse_args()
    if not WAN_IP or not os.environ.get("CHR_PASSWORD"):
        sys.exit("CHR_PASSWORD and CHR_WAN_IP must be set")
    OUT_DIR = args.out
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "identities.json")
    if os.path.exists(path):
        with open(path) as f:
            identities.update(json.load(f))
    print(f"portal listening on {args.bind}:{args.port}, out={OUT_DIR}", file=sys.stderr, flush=True)
    HTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
