"""Browser E2E of the Default Portal: Chromium as real Devices behind the CHR HotSpot.

run.py calls scenario() with its own module, like flash_pairing.scenario. Each Device is one
Playwright container (browser_device.py) on a Docker macvlan over virbr-hs, with its own MAC and
IP, so the Controller resolves it through the Router exactly like a phone. The container asks this
side for Vendo actions over stdin/stdout JSON lines, so the Vendo key never leaves this process.
Rootless: it needs docker, and the CHR lab lock that run.py already holds.

Phases: Device A walks new, Voucher error, online, paused -> Resume and Coin Window -> Done;
Device B takes A's signed cookie and Rebinds; Device A comes back with a fresh browser and runs a
short Voucher out; Device C gets a published Custom Portal whose custom.js throws and whose
inline script the Portal CSP refuses, and still buys time through core.js. Then the Operator's
browser previews three drafts in every sample state from the admin panel, then crops and
uploads a cover and a logo, sees them in the preview and publishes, and Device D gets both.
Finally, the Operator names a Vendo, filters and pages through Sales, restores its history and
exports a CSV matching the filtered JSON, then verifies sign-in, restart persistence and logout. Screenshots go to <report dir>/browser-portal/ for committing.
"""

import json
import os
import re
import importlib.util
import subprocess
import threading
import time
from pathlib import Path

_lab_spec = importlib.util.spec_from_file_location("e2e_lab", Path(__file__).resolve().parents[2] / "lab/chr/e2e_lab.py")
_lab_module = importlib.util.module_from_spec(_lab_spec)
_lab_spec.loader.exec_module(_lab_module)

BASE_IMAGE = "mcr.microsoft.com/playwright/python:v1.64.1-noble"
IMAGE = "openpiso-e2e-browser:pw1.64.1"  # BASE_IMAGE ships the browsers; this layer adds the Python package
NETWORK = "openpiso-e2e-hs"
CONTAINER = "openpiso-e2e-browser"
LEASE_COMMENT = "openpiso-e2e-browser"
DEVICE_A = (_lab_module.BROWSER_MACS[0], "10.5.50.171")
DEVICE_B = (_lab_module.BROWSER_MACS[1], "10.5.50.172")
DEVICE_C = (_lab_module.BROWSER_MACS[2], "10.5.50.173")
DEVICE_D = (_lab_module.BROWSER_MACS[3], "10.5.50.174")
SHORT_SECONDS = 20  # the out-of-time Voucher: long enough to see online, short enough to wait out
PHASE_TIMEOUT = 240


def docker(*args, check=True, **kwargs):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, **kwargs)


def ensure_image() -> None:
    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        dockerfile = f"FROM {BASE_IMAGE}\nRUN pip install --no-cache-dir --break-system-packages playwright==1.64.1\n"
        subprocess.run(["docker", "build", "-q", "-t", IMAGE, "-"], input=dockerfile, text=True, check=True,
                       capture_output=True, timeout=900)


def forget_device(run, address, password, mac, ip) -> None:
    """Remove a Device's HotSpot host, session and static lease; a stale host makes its IP ambiguous."""
    for path, query in (("/ip/hotspot/host", f"address={ip}"), ("/ip/hotspot/host", f"mac-address={mac}"),
                        ("/ip/hotspot/active", f"mac-address={mac}"), ("/ip/dhcp-server/lease", f"mac-address={mac}"),
                        ("/ip/dhcp-server/lease", f"address={ip}")):
        for entry in run.router_rest(address, "admin", password, f"{path}?{query}"):
            run.router_rest(address, "admin", password, f"{path}/{entry['.id']}", "DELETE")


def teardown(run, address, password) -> None:
    for name in ("a", "b", "c", "d", "admin"):
        docker("rm", "-f", f"{CONTAINER}-{name}", check=False)
    docker("network", "rm", NETWORK, check=False)
    for mac, ip in (DEVICE_A, DEVICE_B, DEVICE_C, DEVICE_D):
        forget_device(run, address, password, mac, ip)


def run_device(run, address, password, name, device, phase, inputs, out, hooks, report) -> dict:
    """Run one browser phase as Device (mac, ip) on the HotSpot LAN; return its result."""
    mac, ip = device
    forget_device(run, address, password, mac, ip)
    run.router_rest(address, "admin", password, "/ip/dhcp-server/lease", "PUT", {
        "address": ip, "mac-address": mac, "server": "hs-dhcp", "comment": LEASE_COMMENT})
    network = ["--network", NETWORK, "--ip", ip, "--mac-address", mac.lower(), "--dns", "10.5.50.1"]
    return run_browser(name, network, run.PORTAL_URL, phase, inputs, out, hooks, report)


def run_browser(name, network, controller, phase, inputs, out, hooks, report) -> dict:
    """Run one browser phase in a Playwright container; answer its waits with hooks; return its result."""
    container = f"{CONTAINER}-{name}"
    process = subprocess.Popen(
        ["docker", "run", "--rm", "-i", "--init", "--ipc=host", "--name", container, *network,
         "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
         "-v", f"{Path(__file__).with_name('browser_device.py')}:/work/browser_device.py:ro",
         "-v", f"{out}:/out", IMAGE,
         "python", "-I", "/work/browser_device.py", "--portal", controller, "--phase", phase, "--out", "/out"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    timer = threading.Timer(PHASE_TIMEOUT, lambda: docker("rm", "-f", container, check=False))
    timer.start()
    result = None
    try:
        process.stdin.write(json.dumps(inputs) + "\n")
        process.stdin.flush()
        for line in process.stdout:  # the browser prints only JSON lines; Playwright logs go to stderr
            message = json.loads(line)
            if "wait" in message:
                process.stdin.write(json.dumps(hooks[message["wait"]]()) + "\n")
                process.stdin.flush()
            else:
                result = message["result"]
        assert process.wait() == 0 and result is not None, f"browser phase {phase} failed (exit {process.returncode})"
    finally:
        timer.cancel()
        report.capture_lab_failure()
        if process.poll() is None:
            process.kill()
        docker("rm", "-f", container, check=False)
    return result


def preview_drafts(run, opener) -> dict:
    """Hooks that save each draft the Operator previews: a good one, one with a misspelled marker
    the save check can't know about, and one whose custom.js throws."""
    default = {name: run.request(opener, "/admin/portal/default/" + name).read().decode()
               for name in ("index.html", "portal.css", "custom.js")}
    drafts = {
        "good": {**default, "index.html": default["index.html"].replace("<h1>OpenPiso WiFi</h1>", "<h1>Kanto Piso WiFi</h1>")
                 .replace('<span data-portal="message-text"></span>', "")
                 .replace("</body>", '<p data-portal="valid-until"></p><p data-portal="valid-until"></p></body>'),
                 "custom.js": 'document.addEventListener("portal:state", event => { window.previewState = event.detail; });'},
        "broken-marker": {**default, "index.html": default["index.html"].replace('data-portal="status"', 'data-portal="stauts"')},
        "throwing-custom-js": {**default, "custom.js": "throw new Error('custom.js broke');\n"},
    }

    def save(files):
        def hook():
            assert run.save_portal_draft(opener, files)[0] == 200
            return {}
        return hook
    return {"draft-" + name: save(files) for name, files in drafts.items()}


def scenario(run, opener, address, password, lab, report, restart_controller) -> None:
    request = run.request
    # Site policy for this run: rates, the default Pause Limit, no Idle Pause, 30-day Validity.
    # ₱5 earns 5 Points, which the site Points table trades for 30 minutes.
    assert request(opener, "/admin/rates", "POST", {"tiers": "1=10,2592000,3,1\n5=60,2592000,3,5"}).status == 200
    assert request(opener, "/admin/points", "POST", {"tiers": "5=1800,2592000,2"}).status == 200
    assert request(opener, "/admin/pause", "POST", {"pause_limit": "3", "idle_seconds": "120", "idle_enabled": "off"}).status == 200
    assert request(opener, "/admin/validity", "POST", {"value": "30", "unit": "days"}).status == 200

    def voucher(seconds):
        return run.voucher_codes(opener, run.generate_vouchers(opener, {"seconds": str(seconds), "count": "1"}))[0]

    paired = run.pair_board(opener, lab, lab.coin_board())
    vendo = run.SimulatedVendo(run.BASE_URL, paired["id"], bytes.fromhex(paired["secret"]), timeout=run.HTTP_TIMEOUT)
    assert vendo.heartbeat()[0] == 200

    def coins():
        deadline = time.monotonic() + 10
        while not vendo.window_id:  # the Open arrives on the Vendo's next Report
            assert time.monotonic() < deadline, "Vendo never received Open"
            assert vendo.heartbeat()[0] == 200
            time.sleep(0.2)
        vendo.insert(5)
        assert vendo.heartbeat()[0] == 200
        return {"pesos": 5}

    def closed():
        assert vendo.heartbeat()[0] == 200 and vendo.closing, "Done did not send Close"
        assert vendo.heartbeat()[0] == 200 and not vendo.window_id, "Closed was not acknowledged"
        return {}

    out = report.path.parent / "browser-portal"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    ensure_image()
    teardown(run, address, password)  # leftovers from an interrupted run
    docker("network", "create", "-d", "macvlan", "--subnet", "10.5.50.0/24", "--gateway", "10.5.50.1",
           "-o", "parent=virbr-hs", NETWORK)
    try:
        hooks = {"coins": coins, "closed": closed}
        first = run_device(run, address, password, "a", DEVICE_A, "first", {"voucher": voucher(1800)}, out, hooks, report)
        cookies = first.pop("cookies")
        state = json.load(request(opener, "/admin/state"))
        tc = run.time_credit_for_device(state, *DEVICE_A)
        assert tc["bought_seconds"] == 1800 + 60 + 1800, tc  # the Voucher, ₱5 at 5=60, then 5 Points
        assert tc["points_milli"] == 0, tc
        report.snapshot("browser-portal-new-voucher-pause-coin-window", address, password, opener, device=first)

        rebind = run_device(run, address, password, "b", DEVICE_B, "rebind", {"cookies": cookies}, out, hooks, report)
        state = json.load(request(opener, "/admin/state"))
        moved = run.time_credit_for_device(state, *DEVICE_B)
        assert moved["id"] == tc["id"], moved
        active = run.router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + tc["id"])
        assert [entry["mac-address"].upper() for entry in active] == [DEVICE_B[0]], active
        report.snapshot("browser-portal-rebind-to-new-device", address, password, opener, device=rebind)

        out_of_time = run_device(run, address, password, "a", DEVICE_A, "out", {"voucher": voucher(SHORT_SECONDS)}, out, hooks, report)
        state = json.load(request(opener, "/admin/state"))
        short = run.time_credit_for_device(state, *DEVICE_A)
        assert short["bought_seconds"] == SHORT_SECONDS and short["id"] != tc["id"], short
        assert not run.router_rest(address, "admin", password, "/ip/hotspot/active?user=op-" + short["id"])
        report.snapshot("browser-portal-out-of-time", address, password, opener, device=out_of_time)

        # A Custom Portal from the Default one, with an inline script the save check lets through
        # (the CSP is the boundary) and a custom.js that throws after marking the page.
        files = {name: request(opener, "/admin/portal/default/" + name).read().decode()
                 for name in ("index.html", "portal.css")}
        page = files["index.html"].replace("<h1>OpenPiso WiFi</h1>", "<h1>Kanto Piso WiFi</h1>").replace(
            "</body>", "<script>document.documentElement.dataset.inline = 'ran';</script></body>")
        assert request(opener, "/admin/portal/draft", "POST", {"index_html": page, "portal_css": files["portal.css"],
            "custom_js": "document.documentElement.dataset.custom = 'ran';\nthrow new Error('custom.js broke');\n"}).status == 200
        assert request(opener, "/admin/portal/publish", "POST").status == 200
        try:
            custom = run_device(run, address, password, "c", DEVICE_C, "custom", {"voucher": voucher(600)}, out, hooks, report)
        finally:
            report.capture_lab_failure()
            assert request(opener, "/admin/portal/reset", "POST").status == 200
        state = json.load(request(opener, "/admin/state"))
        bought = run.time_credit_for_device(state, *DEVICE_C)
        assert bought["bought_seconds"] == 600, bought
        report.snapshot("browser-custom-portal-throwing-custom-js", address, password, opener, device=custom)

        # The Operator's browser is on the management network (the host), where admin routes answer.
        published = run.portal_versions(opener)["published"]
        preview = run_browser("admin", ["--network", "host"], run.BASE_URL, "preview",
                              {"password": run.ADMIN_PASSWORD}, out, preview_drafts(run, opener), report)
        assert run.portal_versions(opener)["published"] == published, "previewing published a draft"
        report.snapshot("browser-custom-portal-preview", address, password, opener,
                        preview={draft: {state: {key: row[key] for key in ("read", "unknown", "errors")}
                                         for state, row in rows.items()} for draft, rows in preview["reports"].items()})

        # The Operator brands a clean draft in their own browser, then a Device gets the cover and logo.
        clean = {name: request(opener, "/admin/portal/default/" + name).read().decode()
                 for name in ("index.html", "portal.css", "custom.js")}
        assert request(opener, "/admin/portal/draft", "POST", {"index_html": clean["index.html"],
            "portal_css": clean["portal.css"], "custom_js": clean["custom.js"]}).status == 200
        try:
            operator = run_browser("admin", ["--network", "host"], run.BASE_URL, "upload",
                                   {"password": run.ADMIN_PASSWORD}, out, hooks, report)
            branded = run_device(run, address, password, "d", DEVICE_D, "branded", operator["images"], out, hooks, report)
        finally:
            report.capture_lab_failure()
            assert request(opener, "/admin/portal/reset", "POST").status == 200
        report.snapshot("browser-custom-portal-cover-logo", address, password, opener, device=branded)

        # Real Voucher redemptions make a later page even in --only browser, on its fresh ledger.
        device_ip = lab.device_ip()
        codes = run.voucher_codes(opener, run.generate_vouchers(opener, {"seconds": "60", "count": "26", "pesos": "2"}))
        assert len(codes) == 26
        for code in codes:
            redeemed = run.portal_state(run.pause_device_fetch(lab, device_ip, "/portal/api/redeem", {"code": code}))
            run.assert_message(redeemed, "success", "online")
        sales = run_browser("admin", ["--network", "host"], run.BASE_URL, "sales",
                            {"password": run.ADMIN_PASSWORD, "vendo_id": vendo.id, "search": device_ip, "vouchers": codes},
                            out, {}, report)
        report.snapshot("browser-sales-report", address, password, opener, sales=sales)
        signin = run_browser("admin", ["--network", "host"], run.BASE_URL, "signin",
                             {"password": run.ADMIN_PASSWORD}, out,
                             {"restart-controller": restart_controller}, report)
        # Logging out the browser must not invalidate this separate Operator session.
        assert request(opener, "/admin/state").status == 200
        report.snapshot("browser-signin-logout-restart", address, password, opener, signin=signin)
    finally:
        report.capture_lab_failure()
        teardown(run, address, password)
        request(opener, "/admin/vendos/unpair", "POST", {"id": paired["id"]})

    phases = (first, rebind, out_of_time, custom, preview, operator, branded, sales, signin)
    # browser-portal PNGs are committed; fail here, not in the pre-commit hook's 500 KB check.
    heavy = {shot.name: shot.stat().st_size for shot in out.glob("*.png") if shot.stat().st_size > 500_000}
    assert not heavy, f"screenshots over 500 KB: {heavy}"
    report.data["browser_portal"] = {
        "browser": first["browser"],
        "device_profile": first["device_profile"],
        "code_bytes": first["code_bytes"],
        "code_budget_bytes": 40 * 1024,
        "csp_violations": sum(len(phase["csp_violations"]) for phase in phases),
        "csp_probe_refused": first["csp_probe_refused"],
        "custom_portal_inline_script_refused": custom["inline_script_refused"],
        "custom_js_error_left_core_js_working": any("custom.js broke" in error for error in custom["page_errors"]),
        "preview_reports": {draft: {"read_ok": all(row["read"] == "ok" for row in rows.values()),
                                    "unknown_markers": sorted({name for row in rows.values() for name in row["unknown"]}),
                                    "states_with_errors": [state for state, row in rows.items() if row["errors"]]}
                            for draft, rows in preview["reports"].items()},
        "signin": signin,
        "sales_report": {
            "shortcuts": {label: {key: view[key] for key in ("from", "to", "count", "total_pesos")}
                          for label, view in sales["shortcuts"].items()},
            "single_vendo_named": True,
            "search_count": sales["search"]["count"],
            "later_page": sales["later_page"]["page"],
            "reload_back_forward_restored": sales["reload_back_forward_restored"],
            "csv_rows": sales["csv_rows"],
            "csv_matches_filtered_json": sales["csv_matches_filtered_json"],
            "total_pesos": sales["total_pesos"],
            "csp_violations": sales["csp_violations"],
        },
        "preview_live_api_requests": 0,  # browser_device.preview asserts none
        "http_only_cookie_survived_fetch_posts": first["http_only_cookie"],
        "cover_logo_cropped_in_browser": operator["images"],
        "cover_logo_on_device_match": branded["images"] == operator["images"],
        "screenshots": [f"browser-portal/{name}.png" for phase in phases for name in phase["screenshots"]],
    }


def vouchers_mobile(run, report):
    out = report.path.parent / "vouchers-mobile"
    out.mkdir(parents=True, exist_ok=True)
    ensure_image()
    browser = run_browser("admin", ["--network", "host"], run.BASE_URL, "vouchers-mobile",
                          {"password": run.ADMIN_PASSWORD}, out, {}, report)
    report.data["vouchers_mobile"] = {
        "screenshots": [f"vouchers-mobile/{name}.png" for name in browser["screenshots"]], "browser": browser}
    return browser


def voucher_print_sheet(run, report, path, rows):
    """Operator browser on the management network; leave a reprint screenshot."""
    out = report.path.parent / "voucher-print-sheet"
    out.mkdir(parents=True, exist_ok=True)
    ensure_image()
    browser = run_browser("admin", ["--network", "host"], run.BASE_URL, "voucher-print-sheet",
                          {"password": run.ADMIN_PASSWORD, "path": path, "rows": rows}, out, {}, report)
    report.data["voucher_print_sheet"] = {
        "screenshots": [f"voucher-print-sheet/{name}.png" for name in browser["screenshots"]], "browser": browser}
    return browser


def voucher_template_editor(run, report, path, rows):
    out = report.path.parent / "voucher-template-editor"
    out.mkdir(parents=True, exist_ok=True)
    ensure_image()
    browser = run_browser("admin", ["--network", "host"], run.BASE_URL, "voucher-template-editor",
                          {"password": run.ADMIN_PASSWORD, "path": path, "rows": rows}, out, {}, report)
    report.data["voucher_template_editor"] = {
        "screenshots": [f"voucher-template-editor/{name}.png" for name in browser["screenshots"]], "browser": browser}
    return browser
