"""One customer Device driving the Default Portal in Chromium, inside the Playwright container, or
(phase "preview") the Operator previewing Custom Portal drafts in the admin panel.

browser_portal.py starts a Device on the HotSpot LAN and the Operator on the management network. It reads its inputs as one JSON line on stdin,
asks the host for Vendo actions with {"wait": name} lines (one JSON reply each), and ends with one
{"result": ...} line. Each Portal state it reaches is asserted through data-portal-state and
screenshotted into --out. Any failed check raises, so the container exits non-zero.
"""

import argparse
import base64
import csv
import datetime
import io
import json
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright

PROFILE = "Pixel 7"
BUDGET = 40 * 1024  # index.html + portal.css + core.js, before gzip
CODE = ("/portal", "/portal/portal.css", "/portal/core.js")
COOKIE = "openpiso_time_credit"
# Fetches one image slot and decodes it as the browser does; pixel(x, y) samples the decoded image.
MEASURE = """async path => {
  const response = await fetch(path);
  const blob = await response.blob();
  const bitmap = await createImageBitmap(blob);
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const context = canvas.getContext('2d');
  context.drawImage(bitmap, 0, 0);
  const pixel = (x, y) => Array.from(context.getImageData(x, y, 1, 1).data.slice(0, 3));
  return {type: response.headers.get('content-type'), bytes: blob.size, width: bitmap.width, height: bitmap.height,
          corner: pixel(2, 2), bottom: pixel(bitmap.width >> 1, bitmap.height - 10)};
}"""
# The byte size of the data: URL an element shows, as <img src> or CSS background.
MEASURE_ELEMENT = """element => {
  const url = element.src || getComputedStyle(element).backgroundImage.match(/url\\("?(data:[^")]*)/)[1];
  return atob(url.split(',')[1]).length;
}"""
WHITE, GREEN, RED, BLUE = (255, 255, 255), (46, 125, 50), (198, 40, 40), (21, 101, 192)
# Init scripts run before the page's own scripts and outside its CSP, so every refusal is seen.
CSP_LISTENER = ("document.addEventListener('securitypolicyviolation', e =>"
                " console.error('csp-violation ' + e.effectiveDirective + ' ' + e.blockedURI + ' at ' + location.pathname"
                " + ' ' + e.sourceFile + ':' + e.lineNumber + ' ' + (e.target instanceof Element ? e.target.outerHTML.slice(0, 160) : '')))")


def ask(wait: str) -> dict:
    print(json.dumps({"wait": wait}), flush=True)
    return json.loads(sys.stdin.readline())


class Device:
    def __init__(self, page, context, portal: str, out: str):
        self.page, self.context, self.portal, self.out = page, context, portal, out
        self.screenshots: list[str] = []
        self.violations: list[str] = []
        page.on("console", lambda message: message.text.startswith("csp-violation") and self.violations.append(message.text))

    def marker(self, name: str):
        return self.page.locator(f'[data-portal="{name}"]')

    def shown(self, name: str):
        """A value marker may repeat in the page; this is the copy the customer sees."""
        return self.page.locator(f'[data-portal="{name}"]:visible').first

    def action(self, name: str):
        return self.page.locator(f'[data-action="{name}"]:visible').first

    def state(self, name: str, timeout: float = 20000) -> None:
        expect(self.page.locator("html")).to_have_attribute("data-portal-state", name, timeout=timeout)

    def message(self, code: str) -> None:
        expect(self.marker("message")).to_have_attribute("data-code", code)

    def shot(self, name: str, scale: str = "device") -> None:
        # Chromium itself puts an empty style="" on form inputs; only a value would be inline styling.
        inline = self.page.locator('[style]:not([style=""]), style, script:not([src])')
        assert inline.count() == 0, ("inline style or script in the rendered Portal",
                                     inline.evaluate_all("nodes => nodes.map(node => node.outerHTML.slice(0, 200))"))
        self.page.wait_for_timeout(300)  # let sheet and toast transitions settle
        self.page.screenshot(path=f"{self.out}/{name}.png", scale=scale)
        self.screenshots.append(name)

    def api_state(self) -> dict:
        return self.page.evaluate("fetch('/portal/api/state').then(response => response.json())")

    def redeem(self, code: str) -> None:
        self.action("open-voucher").click()
        sheet = self.marker("sheet-voucher")
        sheet.get_by_label("Voucher code").fill(code)
        sheet.get_by_role("button", name="Use Voucher").click()

    def cookie(self) -> dict:
        return next(c for c in self.context.cookies() if c["name"] == COOKIE)


def first(device: Device, inputs: dict) -> dict:
    """New Device: Voucher error, online, paused -> Resume, Coin Window -> Done."""
    page = device.page
    page.goto(device.portal + "/portal")
    device.state("new")
    expect(device.marker("status")).to_contain_text("From ₱1")
    device.shot("01-new")
    code_bytes = {path: len(page.request.get(device.portal + path).body()) for path in CODE}
    assert sum(code_bytes.values()) <= BUDGET, code_bytes

    device.redeem("ZZZZZZZZ")
    device.state("error")
    device.message("voucher-unknown")
    device.shot("02-voucher-error")

    device.marker("sheet-voucher").get_by_label("Voucher code").fill(inputs["voucher"])
    device.marker("sheet-voucher").get_by_role("button", name="Use Voucher").click()
    device.state("online")
    expect(device.marker("sheet-voucher")).not_to_be_visible()
    expect(device.marker("remaining")).to_have_text(re.compile(r"^00:(29|30):\d\d$"))
    device.shot("03-online")
    # The signed cookie arrived on a fetch POST, and page scripts cannot read it.
    issued = device.cookie()
    assert issued["httpOnly"] and COOKIE not in page.evaluate("document.cookie"), issued

    device.action("pause").click()
    device.state("paused")
    device.message("paused")
    expect(device.shown("pauses-left")).to_have_text("2")
    device.shot("04-paused")
    device.action("resume").click()  # Resume needs the signed cookie: it must ride on the fetch POST
    device.state("online")
    device.message("online")
    device.shot("05-resumed")
    http_only_cookie = device.cookie()["httpOnly"] and device.cookie()["value"] == issued["value"]

    device.action("insert-coin").click()  # one Vendo serves this network: no chooser
    device.state("coin")
    ask("coins")
    expect(device.marker("coins")).to_have_text("₱5")
    # Until the Controller credits the window, the time is a labelled estimate from the rates (₱5 at 5=60).
    expect(device.marker("coin-credit")).to_have_text("About +1 min")
    device.shot("06-coin-window")
    with device.page.expect_response("**/portal/api/coins/done"):  # the Vendo hears Close only after Done
        device.action("coins-done").click()
    ask("closed")
    device.state("online")
    assert device.api_state()["time_credit"]["bought"] == 1800 + 60
    device.shot("07-coin-done")

    # ₱5 earned 5 Points; the site Points table sells 30 minutes for 5. Redeeming
    # works once, then the empty balance is refused with a message.
    expect(device.shown("points")).to_have_text("5")
    device.action("open-points").click()
    sheet = device.marker("sheet-points")
    expect(sheet.locator("tr")).to_have_count(1)
    expect(sheet.locator("tr").first).to_contain_text("30 min")
    device.shot("07a-points")
    sheet.get_by_role("button", name="Redeem").click()
    device.message("online")
    device.state("online")
    expect(sheet).not_to_be_visible()
    expect(device.shown("points")).to_have_text("0")
    assert device.api_state()["time_credit"]["bought"] == 1800 + 60 + 1800
    device.action("open-points").click()
    sheet.get_by_role("button", name="Redeem").click()
    device.message("points-too-low")
    expect(device.marker("message")).to_contain_text("Not enough Points")
    device.shot("07b-points-too-low")
    device.page.keyboard.press("Escape")

    assert not device.violations, device.violations
    # Probe: the CSP really refuses an inline style, and this listener sees it.
    page.evaluate("document.head.append(Object.assign(document.createElement('style'), {textContent: 'body{}'}))")
    page.wait_for_timeout(500)
    probe_refused = any("style-src" in violation for violation in device.violations)
    device.violations.clear()
    return {"code_bytes": code_bytes, "http_only_cookie": http_only_cookie, "csp_probe_refused": probe_refused,
            "cookies": [device.cookie()]}


def rebind(device: Device, inputs: dict) -> dict:
    """A new Device with the old one's signed cookie: Rebind, then online here."""
    device.context.add_cookies(inputs["cookies"])
    held = []
    device.page.route("**/portal/api/connect", lambda route: held.append(route))  # hold the automatic Connect
    device.page.goto(device.portal + "/portal")
    device.state("rebind")
    deadline = time.monotonic() + 10
    while not held:
        assert time.monotonic() < deadline, "core.js did not POST connect for a Rebind"
        device.page.wait_for_timeout(100)
    device.shot("08-rebind")
    held[0].continue_()
    device.state("online")
    device.message("online")
    device.shot("09-rebind-online")
    return {}


def out(device: Device, inputs: dict) -> dict:
    """A fresh browser on the first Device: a short Voucher counts down to out of time."""
    device.page.goto(device.portal + "/portal")
    device.state("new")
    device.redeem(inputs["voucher"])
    device.state("online")
    device.state("out", timeout=90000)  # the countdown's zero asks the Router, the only meter
    expect(device.marker("remaining")).to_have_text("Time's up")
    device.shot("10-out-of-time")
    return {}


def custom(device: Device, inputs: dict) -> dict:
    """A published Custom Portal whose custom.js throws: core.js still runs every flow, and the
    Portal CSP still refuses the page's inline script."""
    errors = []
    device.page.on("pageerror", lambda error: errors.append(str(error)))
    device.page.goto(device.portal + "/portal")
    expect(device.page.locator("h1")).to_have_text("Kanto Piso WiFi")
    device.state("new")
    root = device.page.locator("html")
    expect(root).to_have_attribute("data-custom", "ran")  # custom.js ran up to its throw
    deadline = time.monotonic() + 5
    while not any("custom.js broke" in error for error in errors):
        assert time.monotonic() < deadline, ("custom.js did not throw", errors)
        device.page.wait_for_timeout(100)
    assert root.get_attribute("data-inline") is None, "the CSP let an inline script run"
    assert any("script-src" in violation for violation in device.violations), device.violations
    device.violations.clear()
    device.redeem(inputs["voucher"])
    device.state("online")
    device.message("online")
    # The refused inline script stays in the DOM; drop it so shot() can still check for new ones.
    device.page.evaluate("document.querySelectorAll('script:not([src])').forEach(script => script.remove())")
    device.shot("11-custom-portal-throwing-custom-js")
    return {"page_errors": errors, "inline_script_refused": True}


def near(pixel: list, colour: tuple) -> bool:
    return all(abs(a - b) <= 24 for a, b in zip(pixel, colour))


def source_png(page, width: int, height: int, rects: list) -> bytes:
    """A PNG drawn in the page from (colour, x, y, w, h) rectangles, colour "noise" for 4 px blocks
    of seeded random colour: a stand-in for an Operator's photo."""
    url = page.evaluate("""([width, height, rects]) => {
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext('2d');
      let seed = 1;
      const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) % 256;
      for (const [colour, x, y, w, h] of rects) {
        if (colour !== 'noise') { context.fillStyle = colour; context.fillRect(x, y, w, h); continue; }
        for (let by = y; by < y + h; by += 4) for (let bx = x; bx < x + w; bx += 4) {
          context.fillStyle = `rgb(${random()},${random()},${random()})`;
          context.fillRect(bx, by, 4, 4);
        }
      }
      return canvas.toDataURL('image/png');
    }""", [width, height, rects])
    return base64.b64decode(url.split(",", 1)[1])


def upload(device: Device, inputs: dict) -> dict:
    """The Operator crops and uploads a logo and a cover in the admin page, sees them in the
    preview, then publishes. The only test of the in-browser WebP conversion."""
    page = device.page
    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    page.goto(device.portal + "/admin/portal")
    # A 900×600 logo whose green side bands only a centred square crop removes, and a 2400×1600
    # cover, red on top and blue below, so the focus point decides what the 3:1 crop keeps. The
    # cover's noise band is too detailed for 100 KB at the first quality, so the encoder must step down.
    sources = {"logo": source_png(page, 900, 600, [("#2e7d32", 0, 0, 900, 600), ("#ffffff", 150, 0, 600, 600), ("#1b5e20", 350, 200, 200, 200)]),
               "cover": source_png(page, 2400, 1600, [("#c62828", 0, 0, 2400, 800), ("noise", 0, 200, 2400, 400),
                                                      ("#1565c0", 0, 800, 2400, 800)])}
    for slot, label in (("logo", "Logo"), ("cover", "Cover")):
        box = page.locator(f'[data-portal-image="{slot}"]')
        box.get_by_label(f"{label} image").set_input_files({"name": slot + ".png", "mimeType": "image/png", "buffer": sources[slot]})
        if slot == "cover":
            box.get_by_label("Focus").fill("0")  # the top: all red
        with page.expect_navigation(), page.expect_response(f"**/admin/portal/images/{slot}") as response:
            box.get_by_role("button", name=f"Upload {slot}").click()
        assert response.value.status == 200, response.value.text()

    images = {slot: page.evaluate(MEASURE, "/admin/portal/draft/" + slot) for slot in ("logo", "cover")}
    logo, cover = images["logo"], images["cover"]
    assert logo["type"] == "image/webp" and logo["bytes"] <= 20 * 1024 and (logo["width"], logo["height"]) == (256, 256), logo
    assert near(logo["corner"], WHITE), ("the logo was not cropped to its centred square", logo)
    assert cover["type"] == "image/webp" and cover["bytes"] <= 100 * 1024 and (cover["width"], cover["height"]) == (1200, 400), cover
    assert near(cover["corner"], RED) and near(cover["bottom"], RED), ("the cover crop ignored the focus point", cover)
    page.wait_for_timeout(300)
    # Admin pages are not held to shot()'s Portal rules. CSS scale keeps the cover's noise under the 500 KB hook.
    page.screenshot(path=f"{device.out}/15-admin-cover-logo.png", scale="css")
    device.screenshots.append("15-admin-cover-logo")

    # The sandboxed preview can't send the admin session, so it gets the draft's images inline.
    frame = page.frame_locator("[data-preview] iframe")
    expect(frame.locator("html")).to_have_attribute("data-portal-state", "new")
    logo_element = frame.locator("img.logo")
    expect(logo_element).to_have_js_property("complete", True)
    assert logo_element.evaluate("img => img.naturalWidth") == 256
    assert logo_element.get_attribute("src").startswith("data:image/webp;base64,")
    cover_element = frame.locator(".cover")
    assert "data:image/webp;base64," in cover_element.evaluate("cover => getComputedStyle(cover).backgroundImage")
    previewed = {slot: element.evaluate(MEASURE_ELEMENT) for slot, element in (("logo", logo_element), ("cover", cover_element))}
    assert previewed == {slot: images[slot]["bytes"] for slot in images}, ("the preview shows other images than the draft", previewed)
    page.locator("[data-preview] iframe").screenshot(path=f"{device.out}/16-preview-cover-logo.png", scale="css")
    device.screenshots.append("16-preview-cover-logo")

    with page.expect_navigation():
        page.get_by_role("button", name="Publish draft").click()
    expect(page.get_by_text(re.compile(r"Published: version \d+"))).to_be_visible()
    return {"images": images}


def branded(device: Device, inputs: dict) -> dict:
    """A Device gets the published cover and logo in place of the built-in ones."""
    page = device.page
    page.goto(device.portal + "/portal")
    device.state("new")
    page.wait_for_function("document.querySelector('img.logo').complete")
    images = {slot: page.evaluate(MEASURE, "/portal/" + slot) for slot in ("logo", "cover")}
    assert images == inputs, ("a Device got other images than the Operator published", images, inputs)
    assert page.evaluate("document.querySelector('img.logo').naturalWidth") == 256
    # The page itself loaded both: the logo <img> and the cover's CSS background.
    loaded = page.evaluate("performance.getEntriesByType('resource').map(entry => new URL(entry.name).pathname)")
    assert "/portal/logo" in loaded and "/portal/cover" in loaded, loaded
    device.shot("17-device-cover-logo", scale="css")  # at device scale the cover's noise exceeds the 500 KB hook
    return {"images": images}


PREVIEW_STATES = ("new", "online", "coin", "paused", "out", "error")
DRAFTS = ("good", "broken-marker", "throwing-custom-js")


def preview(device: Device, inputs: dict) -> dict:
    """The Operator previews three saved drafts in every sample state from the admin panel. The
    sandboxed preview gets sample states, so the browser never asks the live Portal API, and the
    report shows a misspelled marker and a throwing custom.js."""
    page = device.page
    urls = []
    page.on("request", lambda request: urls.append(request.url))
    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(device.portal + "/admin/dashboard")
    reports = {}
    for number, draft in enumerate(DRAFTS, start=12):
        ask("draft-" + draft)
        page.goto(device.portal + "/admin/portal")
        card = page.locator("[data-preview]")
        card.get_by_role("button", name="Check every state").click()
        expect(card.locator("tr[data-state][data-read]")).to_have_count(len(PREVIEW_STATES), timeout=30000)
        reports[draft] = {row["state"]: row for row in card.locator("tr[data-state]").evaluate_all(
            """rows => rows.map(row => {
                 const items = (field) => [...row.querySelectorAll(`[data-field=${field}] li`)].map(li => li.textContent);
                 return {state: row.dataset.state, read: row.dataset.read,
                         bound: items("bound"), unknown: items("unknown"), errors: items("errors")};
               })""")}
        assert tuple(reports[draft]) == PREVIEW_STATES, reports[draft]
        # The picker shows one state; core.js in the sandbox renders it from the sample.
        card.get_by_role("button", name="paused", exact=True).click()
        frame = page.frame_locator("[data-preview] iframe")
        expect(frame.locator("html")).to_have_attribute("data-portal-state", "paused")
        if draft == "good":
            # Optional value markers repeat, messages need no nested message-text, and Validity uses the browser locale.
            for state in ("new", "online", "paused", "error"):
                card.get_by_role("button", name=state, exact=True).click()
                expect(frame.locator("html")).to_have_attribute("data-portal-state", state)
                expected = frame.locator("html").evaluate("""() => {
                    const tc = window.previewState.time_credit;
                    return tc ? new Intl.DateTimeFormat(undefined, {
                      month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
                    }).format(new Date(tc.valid_until * 1000)) : "";
                }""")
                expect(frame.locator('[data-portal="valid-until"]')).to_have_text([expected, expected])
                if state in ("online", "paused"):
                    expect(frame.locator('[data-portal="status"]')).to_contain_text(expected)
                if state == "error":
                    text = frame.locator("html").evaluate("window.previewState.message.text")
                    expect(frame.locator('[data-portal="message"]')).to_have_text(text)
                    expect(frame.locator('[data-portal="message-text"]')).to_have_count(0)
            card.get_by_role("button", name="paused", exact=True).click()
            expect(frame.locator("html")).to_have_attribute("data-portal-state", "paused")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{device.out}/{number}-preview-{draft}.png", full_page=True)
        device.screenshots.append(f"{number}-preview-{draft}")

    for state, row in reports["good"].items():
        assert row["read"] == "ok" and not row["unknown"] and not row["errors"], row
        assert {"remaining", "message", "resume-code", "valid-until", "insert-coin", "redeem", "recover"} <= set(row["bound"]), row
    for state, row in reports["broken-marker"].items():
        assert row["read"] == "ok" and row["unknown"] == ["stauts"] and not row["errors"], row
    for state, row in reports["throwing-custom-js"].items():
        assert row["read"] == "ok" and not row["unknown"], row
        assert any("custom.js broke" in error for error in row["errors"]), row
    # Only the Controller's admin pages and Portal files: no live Portal API, no Router.
    live = [url for url in urls if "/portal/api/" in url or not url.startswith((device.portal + "/", "data:", "about:"))]
    assert not live, live
    # Sandboxing and connect-src do not stop a draft from navigating its own iframe.
    with page.expect_response("**/portal/api/state") as navigation:
        frame.locator("html").evaluate("location.href = '/portal/api/state'")
    # frame-ancestors 'none' keeps Chromium from rendering the 403 body; run.py checks it over HTTP.
    assert navigation.value.status == 403, navigation.value.status
    return {"reports": reports, "requests": len(urls), "api_navigation_status": navigation.value.status,
            "message_fallback": True, "valid_until_locale": "de-DE"}


def sales(device: Device, inputs: dict) -> dict:
    """An Operator names a station, browses bookmarkable Sales, and exports the filtered report."""
    page = device.page
    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    # The Vendo List selects a Vendo by ?vendo= and edits it in a dialog that saves in place (#100).
    page.goto(device.portal + f"/admin/vendos?vendo={inputs['vendo_id']}")
    page.locator('[data-detail-slot] .vdetail').get_by_role("button", name="Edit", exact=True).click()
    dialog = page.locator("dialog.vendo-editor")
    dialog.get_by_label("Name", exact=True).fill("Browser station")
    dialog.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator('[data-detail-slot] .vdetail')).to_have_attribute("data-name", "Browser station")
    page.keyboard.press("Escape")
    page.get_by_role("link", name="Sales Inventory", exact=True).click()
    report = page.locator("#sales-report")
    expect(report.locator(".sales-totals span")).to_have_text(["Total Sales", "Vendo Sales", "Voucher Sales"])
    # A property on window survives htmx swaps, but not a document navigation.
    page.evaluate("window.salesDocument = 'same document'")

    def readback():
        response = page.request.get(page.url + ("&" if "?" in page.url else "?") + "format=json")
        assert response.ok, response.status
        data = response.json()
        expect(report.get_by_label("From", exact=True)).to_have_value(data["from"])
        expect(report.get_by_label("To", exact=True)).to_have_value(data["to"])
        expect(report.get_by_role("combobox", name="Vendo", exact=True, include_hidden=True)).to_have_value(str(data["vendo_id"] or "all"))
        expect(report.get_by_label("Search", exact=True)).to_have_value(data["q"])
        expect(report.get_by_role("combobox", name="Entries", exact=True, include_hidden=True)).to_have_value(str(data["page_size"]))
        expect(report.locator(".sales-total strong")).to_have_text(f"₱{data['total_pesos']}")
        first = (data["page"] - 1) * data["page_size"] + 1 if data["count"] else 0
        last = min(data["page"] * data["page_size"], data["count"])
        expect(report.locator(".card-foot p")).to_have_text(f"Showing {first} to {last} of {data['count']} entries")
        mode = report.get_attribute("data-view")
        if mode == "table":
            expect(report.locator("tbody tr")).to_have_count(len(data["rows"]) or 1)
            assert report.locator("tbody td[title]").evaluate_all("nodes => nodes.map(n => n.title)") == [row["user"] for row in data["rows"]]
        else:
            assert report.locator("dd[data-user]").all_text_contents() == [row["user"] for row in data["rows"]]
        return data

    def update(action):
        panel = report.locator(".sales-filter-panel")
        if panel.count():
            panel.evaluate("node => node.open = true")
        with page.expect_response(lambda response: "/admin/sales?" in response.url and response.request.headers.get("hx-request") == "true") as response:
            action()
        assert response.value.status == 200
        # Wait for both the swap and history push, not just the response headers.
        expected = urlsplit(response.value.url)
        page.wait_for_url(lambda url: urlsplit(url).query == expected.query)
        assert page.evaluate("window.salesDocument") == "same document", "filter reloaded the document"
        return readback()

    def shot(name):
        page.screenshot(path=f"{device.out}/{name}.png", full_page=True)
        device.screenshots.append(name)

    expect(report).to_have_attribute("data-view", "table")
    views = {}
    for label in ("Today", "This week", "This month", "This year"):
        link = report.get_by_role("link", name=label, exact=True)
        expected = parse_qs(urlsplit(link.get_attribute("href")).query)
        data = update(link.click)
        today = datetime.date.fromisoformat(data["to"])
        starts = {"Today": today, "This week": today - datetime.timedelta(days=today.weekday()),
                  "This month": today.replace(day=1), "This year": today.replace(month=1, day=1)}
        assert data["from"] == starts[label].isoformat() and data["page"] == 1
        assert parse_qs(urlsplit(page.url).query) == expected
        views[label] = data
    assert data["count"] > 25, data
    shot("30-sales-all")
    all_total = data["total_pesos"]
    single = update(lambda: report.get_by_role("combobox", name="Vendo", exact=True).select_option(str(inputs["vendo_id"])))
    assert single["vendo_id"] == inputs["vendo_id"] and single["page"] == 1
    assert single["count"] == 1 and single["total_pesos"] == 5
    assert all(row["vendo_name"] == "Browser station" and row["type"] == "coin" for row in single["rows"])
    expect(report.locator("tbody")).to_contain_text("Browser station")
    shot("31-sales-single-vendo")
    update(lambda: report.get_by_role("combobox", name="Vendo", exact=True).select_option("all"))
    report.locator(".sales-filter-panel").evaluate("node => node.open = true")
    report.get_by_label("Search", exact=True).fill(inputs["search"])
    searched = update(lambda: report.get_by_role("button", name="Show Sales").click())
    assert searched["q"] == inputs["search"] and 26 <= searched["count"] < data["count"]
    assert searched["total_pesos"] == all_total
    shot("32-sales-search")
    for size in (50, 100, 25):
        data = update(lambda: report.get_by_role("combobox", name="Entries", exact=True).select_option(str(size)))
        assert data["page_size"] == size and data["page"] == 1
    first_url = page.url
    first_view = readback()
    later = update(lambda: report.get_by_role("link", name="2", exact=True).click())
    assert later["page"] == 2 and len(later["rows"]) == min(25, later["count"] - 25)
    later_url = page.url
    shot("33-sales-later-page")
    page.reload()
    expect(page.get_by_role("link", name="Vendo List", exact=True)).to_be_visible()
    assert page.url == later_url and readback() == later
    page.go_back()
    page.wait_for_url(first_url)
    assert readback() == first_view
    page.go_forward()
    page.wait_for_url(later_url)
    assert readback() == later

    # Export from page 2 must contain every match, not just the visible page.
    with page.expect_download() as downloading:
        report.get_by_role("link", name="Export", exact=True).click()
    download = downloading.value
    assert download.failure() is None
    assert download.suggested_filename == f"sales-{later['from']}-to-{later['to']}.csv"
    with open(download.path(), "rb") as downloaded:
        raw = downloaded.read()
    assert raw.startswith(b"\xef\xbb\xbf")
    records = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    rows = []
    query = parse_qs(urlsplit(page.url).query)
    query["format"], query["page_size"] = ["json"], ["100"]
    for number in range(1, (later["count"] + 99) // 100 + 1):
        query["page"] = [str(number)]
        exported = page.request.get(device.portal + "/admin/sales?" + urlencode(query, doseq=True)).json()
        assert exported["total_pesos"] == later["total_pesos"] == all_total
        rows.extend(exported["rows"])
    assert len(records) == len(rows) == later["count"]
    assert set(inputs["vouchers"]) <= {row["user"] for row in rows}
    assert all(row["amount"] == 2 and row["credited_seconds"] == 60 for row in rows if row["user"] in inputs["vouchers"])
    for number, (record, row) in enumerate(zip(records, rows), 1):
        at = datetime.datetime.fromisoformat(row["at"])
        # The full run may have left formula-leading station names; CSV neutralizes them.
        name, seller = (("'" + cell) if cell.startswith(("\t", "\r", "\n")) or cell.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) else cell
                        for cell in (row["vendo_name"], row["seller"]))
        assert record == {
            "#": str(number), "Date": at.strftime("%Y-%m-%d"), "Time": at.strftime("%H:%M:%S"),
            "IP": row["ip"], "MAC": row["mac"], "User": row["user"], "Validity": row["validity"],
            "Type": "Coin" if row["type"] == "coin" else "Voucher", "Vendo": name, "seller": seller,
            "Credited time (seconds)": str(row["credited_seconds"]),
            "Amount (PHP)": str(row["amount"]) if row["amount"] is not None else "",
        }
    expect(report.locator(".sales-total strong")).to_have_text(f"₱{all_total}")
    page.evaluate("window.salesDocument = 'same document'")
    report.locator(".sales-filter-panel").evaluate("node => node.open = true")
    report.get_by_label("Search", exact=True).fill("")
    reset = update(lambda: report.get_by_role("combobox", name="Vendo", exact=True).select_option(str(inputs["vendo_id"])))
    assert reset["page"] == 1 and reset["vendo_id"] == inputs["vendo_id"] and reset["q"] == "" and reset["count"] == 1
    # #80: both layouts are bookmarkable, preserve filters, and fit a phone.
    table_url = page.url
    table_data = readback()
    feed = update(lambda: report.get_by_role("link", name="Feed", exact=True).click())
    assert feed == table_data
    expect(report).to_have_attribute("data-view", "feed")
    assert parse_qs(urlsplit(page.url).query)["view"] == ["feed"]
    expect(report.locator(".sales-receipt")).to_have_count(len(feed["rows"]))
    feed_url = page.url
    page.reload()
    expect(report).to_have_attribute("data-view", "feed")
    assert page.url == feed_url and readback() == feed
    page.go_back()
    page.wait_for_url(table_url)
    expect(report).to_have_attribute("data-view", "table")
    assert readback() == table_data
    page.evaluate("window.salesDocument = 'same document'")
    desktop = page.viewport_size
    page.set_viewport_size({"width": 390, "height": 844})
    for mode in ("table", "feed"):
        update(lambda: report.get_by_role("link", name=mode.title(), exact=True).click())
        panel = report.locator(".sales-filter-panel")
        if panel.count():
            panel.evaluate("node => node.open = false")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), mode
        details = report.locator(".sales-receipt:visible details").first
        details.locator("summary").click()
        expect(details.locator("dd[data-user]")).to_have_text(table_data["rows"][0]["user"])
        assert details.locator("summary").bounding_box()["height"] >= 44
        shot("34-sales-mobile-" + mode)
        export = report.get_by_role("link", name="Export", exact=True)
        expect(export).to_have_count(1)
        expect(export).to_be_visible()
        with page.expect_download() as mobile_download:
            export.click()
        assert mobile_download.value.failure() is None
        report.get_by_role("button", name="Filter Sales", exact=True).click()
        expect(report.locator("dialog")).to_be_visible()
        expect(report.get_by_role("combobox", name="Vendo", exact=True)).to_be_focused()
        report.get_by_role("button", name="Close filters").click()
    page.set_viewport_size(desktop)
    assert not device.violations, device.violations
    return {"shortcuts": views, "single_vendo": single, "search": searched, "later_page": later,
            "reload_back_forward_restored": True, "csv_rows": len(records), "total_pesos": all_total,
            "csv_matches_filtered_json": True}


def signin(device: Device, inputs: dict) -> dict:
    """Signed-out deep link, inline error, durable login, and this browser's logout."""
    page = device.page

    # Not Device.shot: its no-inline-style check is for the Portal, and Alpine's x-show
    # legitimately sets style="display: none" on the admin shell's menus.
    def shot(name):
        page.screenshot(path=f"{device.out}/{name}.png")
        device.screenshots.append(name)

    target = device.portal + "/admin/sales?from=1970-01-01&to=2099-12-31&q=signin%20flow&view=feed"
    page.goto(target)
    assert urlsplit(page.url).path == "/admin/login"
    assert parse_qs(urlsplit(page.url).query)["next"] == [target.removeprefix(device.portal)]
    expect(page.get_by_text("Log in to continue to Sales Inventory", exact=True)).to_be_visible()
    shot("35-signin-continue-sales")
    page.get_by_label("Password", exact=True).fill("wrong")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(page.get_by_label("Password", exact=True)).to_have_attribute("type", "text")
    page.get_by_role("button", name="Hide", exact=True).click()
    expect(page.get_by_label("Password", exact=True)).to_have_attribute("type", "password")
    with page.expect_navigation() as wrong:
        page.get_by_role("button", name="Log in", exact=True).click()
    assert wrong.value.status == 401
    expect(page.get_by_role("alert")).to_have_text("Wrong password.")
    expect(page.locator('input[name="next"]')).to_have_value(target.removeprefix(device.portal))
    shot("36-signin-wrong-password")
    page.get_by_label("Password", exact=True).fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in", exact=True).click()
    assert page.url == target
    expect(page.locator("#sales-report")).to_be_visible()
    shot("37-signin-returned-to-sales")
    cookie = next(cookie for cookie in device.context.cookies() if cookie["name"] == "openpiso_admin")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    ask("restart-controller")
    page.reload()
    assert page.url == target
    expect(page.locator("#sales-report")).to_be_visible()
    after = next(cookie for cookie in device.context.cookies() if cookie["name"] == "openpiso_admin")
    assert after["value"] == cookie["value"] and after["expires"] == cookie["expires"]
    shot("38-signin-survived-restart")
    page.locator(".sb-footer .user > button").click()
    with page.expect_navigation():
        page.get_by_role("menuitem", name="Log out", exact=True).click()
    expect(page.get_by_text("You've logged out.", exact=True)).to_be_visible()
    assert not any(cookie["name"] == "openpiso_admin" for cookie in device.context.cookies())
    shot("39-signin-logged-out")
    page.goto(target)
    expect(page.get_by_text("Log in to continue to Sales Inventory", exact=True)).to_be_visible()
    assert urlsplit(page.url).path == "/admin/login"
    return {"exact_next_preserved": True, "wrong_password_status": 401,
            "session_survived_restart": True, "session_not_renewed": True, "logged_out": True}


def vendos(device: Device, inputs: dict) -> dict:
    """Select a Vendo, edit structured Rates, Undo Deactivate and Unpair, then
    Unpair after the real grace period; repeat the detail flow in a phone sheet."""
    page = device.page

    def shot(name):  # Alpine's x-show sets inline styles in the admin shell, as in signin()
        page.screenshot(path=f"{device.out}/{name}.png")
        device.screenshots.append(name)

    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    page.goto(device.portal + "/admin/vendos")
    expect(page.get_by_role("heading", level=3, name="Vendo List", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Vendo List", exact=True)).to_be_visible()
    shot("01-vendo-list")
    vendo_id = inputs["vendo_id"]
    item = page.locator(f'[data-select="{vendo_id}"]')
    detail = page.locator('[data-detail-slot] .vdetail')
    empty_pane = page.get_by_role("heading", name="Select a Vendo", exact=True)
    expect(empty_pane).to_be_visible()
    expect(page.locator('[data-select][aria-current="true"]')).to_have_count(0)
    expect(detail).to_have_count(0)
    item.click()
    expect(item).to_have_attribute("aria-current", "true")
    expect(detail).to_have_attribute("data-id", str(vendo_id))
    assert parse_qs(urlsplit(page.url).query)["vendo"] == [str(vendo_id)]
    shot("02-vendo-selected-detail")
    detail.get_by_role("button", name="Close", exact=True).click()
    expect(empty_pane).to_be_visible()
    expect(detail).to_have_count(0)
    expect(item).to_have_attribute("aria-current", "false")
    expect(item).to_be_focused()
    assert "vendo" not in parse_qs(urlsplit(page.url).query)
    item.click()
    detail.get_by_role("button", name="Edit", exact=True).click()
    dialog = page.locator("dialog.vendo-editor")
    expect(dialog.get_by_label("Name", exact=True)).to_be_focused()
    expect(dialog.get_by_label("IP", exact=True)).not_to_be_editable()
    expect(dialog.get_by_label("MAC", exact=True)).not_to_be_editable()
    shot("03-vendo-edit-dialog")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    detail.get_by_role("button", name="Edit", exact=True).click()
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_be_hidden()

    # Deactivate is immediate, while Undo restores the persisted active flag.
    detail.get_by_role("button", name="Deactivate", exact=True).click()
    page.locator("dialog.vendo-confirm").get_by_role("button", name="Deactivate", exact=True).click()
    expect(detail).to_have_attribute("data-status", "Deactivated")
    shot("04-vendo-deactivated-undo")
    page.locator(".vendo-toast").filter(has_text="deactivated").get_by_role("button", name="Undo", exact=True).click()
    expect(detail).not_to_have_attribute("data-status", "Deactivated")
    state = page.request.get(device.portal + "/admin/state").json()
    assert next(v for v in state["vendos"] if v["id"] == vendo_id)["active"] is True

    unpair_posts = []
    page.on("request", lambda request: unpair_posts.append(time.monotonic())
            if request.method == "POST" and urlsplit(request.url).path == "/admin/vendos/unpair" else None)

    button = page.locator('[data-hold]:visible')
    button.focus()
    page.keyboard.down("Space")
    try:
        expect(button).to_have_attribute("data-holding", "true")
        page.wait_for_timeout(500)
    finally:
        page.keyboard.up("Space")
    expect(button).not_to_have_attribute("data-holding", "true")
    expect(page.locator(".vendo-toast").filter(has_text=re.compile(r"^Unpairing .* in 30s"))).to_have_count(0)
    assert not unpair_posts, "Releasing the hold early posted Unpair"

    def hold():
        button = page.locator('[data-hold]:visible')
        button.focus()
        page.keyboard.down("Space")
        try:
            expect(page.get_by_text("Unpair scheduled. Undo is available for 30 seconds.", exact=True)).to_be_visible(timeout=6000)
        finally:
            page.keyboard.up("Space")

    hold()
    assert not unpair_posts, "Unpair posted before the grace period"
    shot("05-vendo-unpair-pending")
    page.locator(".vendo-toast").filter(has_text=re.compile(r"^Unpairing .* in 30s")).get_by_role("button", name="Undo", exact=True).click()
    expect(page.get_by_text("Unpair cancelled", exact=True)).to_be_visible()
    assert next(v for v in page.request.get(device.portal + "/admin/state").json()["vendos"] if v["id"] == vendo_id)["status"] == "paired"

    # A phone opens the same detail and editor in a bottom sheet, without overflow.
    page.set_viewport_size({"width": 390, "height": 844})
    sheet = page.get_by_role("dialog", name="Vendo details", exact=True)
    expect(sheet).to_be_visible()
    expect(sheet.locator(".vdetail")).to_have_attribute("data-id", str(vendo_id))
    shot("06-vendo-phone-sheet")
    sheet.get_by_role("button", name="Edit", exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog.locator(".sheet-edit-titles h3")).to_be_focused()
    dialog.get_by_role("tab", name="Rate tiers", exact=True).click()
    rates = dialog.locator('[data-edit-panel="rates"]')
    rates.get_by_role("button", name="Customize for this Vendo", exact=True).click()
    expect(rates.locator('[data-tiers="rates"]')).to_be_visible()
    rates.get_by_role("button", name="Add tier", exact=True).click()
    tier = rates.locator(".tier").last
    tier.locator('[name="tier_price"]').fill("1000")
    tier.locator('[name="tier_minutes"]').fill("100000")
    tier.locator('[name="tier_points"]').fill("1000000")
    tier.locator('[name="tier_validity"]').fill("2")
    tier.get_by_label("Validity unit", exact=True).select_option("weeks")
    tier.locator('[name="tier_pause"]').fill("4")
    rate_posts = []
    page.on("request", lambda request: rate_posts.append(request.url)
            if request.method == "POST" and urlsplit(request.url).path == "/admin/vendos/rates" else None)
    tier.locator('[name="tier_price"]').fill(rates.locator('.tier [name="tier_price"]').first.input_value())
    rates.get_by_role("button", name="Save tiers", exact=True).click()
    expect(rates.locator('[data-save-error]')).to_contain_text("Price duplicates another row.")
    expect(tier.locator('[name="tier_price"]')).to_have_attribute("aria-invalid", "true")
    assert not rate_posts, "Duplicate price was POSTed instead of rejected client-side"
    shot("07-vendo-phone-duplicate-price-rejected")
    tier.locator('[name="tier_price"]').fill("1000")
    shot("07-vendo-phone-structured-rates")
    draft = tier.element_handle()
    with page.expect_response(lambda response: urlsplit(response.url).path == "/admin/vendos/rates" and response.request.method == "POST") as saved:
        rates.get_by_role("button", name="Save tiers", exact=True).click()
    assert saved.value.status in (200, 303), saved.value.status
    draft.wait_for_element_state("hidden")  # the successful save replaces the panel
    expect(rates.locator(".tier").last.locator('[name="tier_price"]')).to_have_value("1000")
    expect(rates.locator('[data-save-error]')).to_have_count(0)
    dialog.locator('[data-edit-close]:visible').click()
    page.reload()
    expect(sheet).to_be_visible()
    sheet.get_by_role("button", name="Edit", exact=True).click()
    expect(dialog).to_be_visible()
    dialog.get_by_role("tab", name="Rate tiers", exact=True).click()
    persisted = dialog.locator('[data-edit-panel="rates"] .tier').last
    expect(persisted.locator('[name="tier_price"]')).to_have_value("1000")
    expect(persisted.locator('[name="tier_minutes"]')).to_have_value("100000")
    expect(persisted.locator('[name="tier_points"]')).to_have_value("1000000")
    expect(persisted.locator('[name="tier_validity"]')).to_have_value("2")
    expect(persisted.locator('[name="tier_unit"]')).to_have_value("weeks")
    expect(persisted.locator('[name="tier_pause"]')).to_have_value("4")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shot("08-vendo-phone-rates-persisted")
    dialog.locator('[data-edit-close]:visible').click()
    sheet.get_by_role("button", name="Close", exact=True).click()
    expect(sheet).to_be_hidden()
    item.click()
    expect(sheet).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 900})
    expect(sheet).to_be_hidden()
    expect(detail).to_have_attribute("data-id", str(vendo_id))

    # Real time, not a mocked clock: neither cancelled deadline nor the new one
    # may issue a POST early. Only the second hold survives to its deadline.
    hold()
    pending_at = time.monotonic()
    page.wait_for_timeout(28000)
    assert not unpair_posts, "Unpair Undo failed or a grace period ended early"
    expect(detail).to_have_attribute("data-status", "Unpaired", timeout=15000)
    assert len(unpair_posts) == 1 and unpair_posts[0] - pending_at >= 29, unpair_posts
    expect(detail.locator('[data-vendo-edit], [data-hold], form[action="/admin/vendos/active"]')).to_have_count(0)
    shot("09-vendo-unpaired-after-grace")
    return {"initial_empty_pane": True, "wide_close_restores_focus": True,
            "short_unpair_hold_cancelled": True, "duplicate_price_rejected_without_post": True,
            "selection_detail": True, "focus_on_name": True, "escape_closes": True,
            "close_button_closes": True, "ip_mac_read_only": True, "deactivate_undo": True,
            "unpair_undo": True, "unpair_real_grace_seconds": unpair_posts[0] - pending_at,
            "phone_sheet": True, "structured_rates_persisted": True}


def vendo_rates(device: Device, inputs: dict) -> dict:
    """The Operator moves between the Edit dialog's three tabs from the keyboard
    (WAI-ARIA Tabs: arrows wrap, Home/End, only the selected tab is in the Tab
    order), customizes the Vendo's rates from the Rates tab, and finds the
    Points tab using the site Points table (#98)."""
    page = device.page

    def shot(name):  # Alpine's x-show sets inline styles in the admin shell, as in signin()
        page.screenshot(path=f"{device.out}/{name}.png")
        device.screenshots.append(name)

    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    page.goto(device.portal + "/admin/vendos")
    page.locator(f'[data-select="{inputs["vendo_id"]}"]').click()
    page.locator('[data-detail-slot]').get_by_role("button", name="Edit", exact=True).click()
    dialog = page.locator("dialog.vendo-editor")
    tablist = dialog.get_by_role("tablist")
    tabs = {label: (tablist.get_by_role("tab", name=label, exact=True), dialog.get_by_role("tabpanel", name=label, exact=True))
            for label in ("Details", "Rate tiers", "Points table")}
    details = tabs["Details"][0]
    rates, rates_panel = tabs["Rate tiers"]
    points, points_panel = tabs["Points table"]

    def selected(label):
        for other, (tab, panel) in tabs.items():
            expect(tab).to_have_attribute("aria-selected", "true" if other == label else "false")
            expect(tab).to_have_attribute("tabindex", "0" if other == label else "-1")
            if other == label:
                expect(tab).to_be_focused()
                expect(panel).to_be_visible()
            else:
                expect(panel).to_be_hidden()

    expect(details).to_have_attribute("aria-selected", "true")
    expect(rates_panel).to_be_hidden()
    expect(points_panel).to_be_hidden()
    # Shift+Tab from the focused Name field reaches the selected tab.
    expect(dialog.get_by_label("Name", exact=True)).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(details).to_be_focused()
    page.keyboard.press("ArrowRight")
    selected("Rate tiers")
    expect(rates_panel).to_contain_text("Using Shop tiers")
    shot("01-rates-tab")
    page.keyboard.press("ArrowRight")
    selected("Points table")
    expect(points_panel).to_contain_text("Using Shop Points table")
    expect(points_panel.get_by_role("button", name="Customize for this Vendo", exact=True)).to_be_visible()
    shot("02-points-tab")
    page.keyboard.press("ArrowRight")  # wraps
    selected("Details")
    page.keyboard.press("ArrowLeft")  # wraps back
    selected("Points table")
    page.keyboard.press("Home")
    selected("Details")
    page.keyboard.press("End")
    selected("Points table")
    page.keyboard.press("ArrowLeft")
    selected("Rate tiers")
    # Tab leaves the tablist for the panel's first control.
    page.keyboard.press("Tab")
    customize = rates_panel.get_by_role("button", name="Customize for this Vendo", exact=True)
    expect(customize).to_be_focused()
    page.keyboard.press("Enter")
    expect(rates_panel.locator('[data-tiers="rates"]')).to_be_visible()
    rates.focus()
    selected("Rate tiers")
    tier = rates_panel.locator(".tier").first
    expect(tier.locator('[name="tier_price"]')).to_have_value("1")
    expect(tier.locator('[name="tier_minutes"]')).to_have_value("1")
    expect(tier.locator('[name="tier_points"]')).to_have_value("0")
    expect(tier.locator('[name="tier_validity"]')).to_have_value("1")
    expect(tier.locator('[name="tier_unit"]')).to_have_value("hours")
    expect(tier.locator('[name="tier_pause"]')).to_have_value("1")
    expect(rates_panel.get_by_role("button", name="Use Shop tiers", exact=True)).to_be_visible()
    shot("03-rates-customized")
    # Customizing Rates leaves the Points tab on the site table.
    points.click()
    selected("Points table")
    expect(points_panel).to_contain_text("Using Shop Points table")
    return {"arrows_move_and_wrap": True, "home_end": True, "roving_tabindex": True,
            "customize_from_keyboard": True, "points_tab_uses_site_table": True}


def shop_vendo_list(device: Device, inputs: dict) -> dict:
    """A Vendo with its own Rate tiers and Points table (#107): its row carries
    the Custom badges; Use Shop tiers and Use Shop Points table each confirm with
    what is discarded, and afterwards the tab offers Customize for this Vendo."""
    page = device.page

    def shot(name):  # Alpine's x-show sets inline styles in the admin shell, as in signin()
        page.screenshot(path=f"{device.out}/{name}.png")
        device.screenshots.append(name)

    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    page.goto(device.portal + "/admin/vendos")
    item = page.locator(f'[data-select="{inputs["vendo_id"]}"]')
    expect(item).to_contain_text("Custom tiers")
    expect(item).to_contain_text("Custom Points")
    item.click()
    shot("01-custom-badges")
    page.locator('[data-detail-slot]').get_by_role("button", name="Edit", exact=True).click()
    dialog = page.locator("dialog.vendo-editor")
    resets = (("Rate tiers", "Use Shop tiers", "This Vendo's own tiers are discarded. Its next Coin Window prices at Shop tiers.",
               "Starts from a copy of the Shop tiers."),
              ("Points table", "Use Shop Points table", "This Vendo's own table is discarded. Redemptions here use the Shop table from now on.",
               "Starts from a copy of the Shop Points table."))
    for n, (tab, button, message, hint) in enumerate(resets, 2):
        dialog.get_by_role("tab", name=tab, exact=True).click()
        panel = dialog.get_by_role("tabpanel", name=tab, exact=True)
        panel.get_by_role("button", name=button, exact=True).click()
        confirm = page.get_by_role("dialog", name=button + "?", exact=True)
        expect(confirm).to_contain_text(message)
        shot(f"0{n}a-confirm-{button.lower().replace(' ', '-')}")
        with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path.startswith("/admin/vendos/")) as saved:
            confirm.get_by_role("button", name=button, exact=True).click()
        assert saved.value.status in (200, 303), saved.value.status
        expect(panel.get_by_role("button", name="Customize for this Vendo", exact=True)).to_be_visible()
        expect(panel).to_contain_text(hint)
        shot(f"0{n}b-after-{button.lower().replace(' ', '-')}")
    return {"confirms": [message for _, _, message, _ in resets], "customize_offered_after_reset": True}

def shop_pills(device: Device, inputs: dict) -> dict:
    """Shop › Rate tiers pills (#105): each pill expands its Vendo list and
    collapses the other, from the mouse and from the keyboard, with
    aria-expanded tracking it; Edit tiers opens the Vendo dialog on its Rate
    tiers tab; Open shows the Vendo's details."""
    page = device.page

    def shot(name):  # Alpine's x-show sets inline styles in the admin shell, as in signin()
        page.screenshot(path=f"{device.out}/{name}.png")
        device.screenshots.append(name)

    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    page.goto(device.portal + "/admin/rates")
    shop, custom = page.locator('[data-group="shop"]'), page.locator('[data-group="custom"]')
    shop_list, custom_list = page.locator("#group-shop"), page.locator("#group-custom")

    def state(open_shop, open_custom):
        expect(shop).to_have_attribute("aria-expanded", "true" if open_shop else "false")
        expect(custom).to_have_attribute("aria-expanded", "true" if open_custom else "false")
        (expect(shop_list).to_be_visible if open_shop else expect(shop_list).to_be_hidden)()
        (expect(custom_list).to_be_visible if open_custom else expect(custom_list).to_be_hidden)()

    state(False, False)
    shop.click()
    state(True, False)
    expect(shop_list).to_contain_text("A change here applies to each from its next Coin Window.")
    shot("01-shop-list")
    custom.click()  # opening one closes the other
    state(False, True)
    expect(custom_list).to_contain_text("Shop changes do not affect these.")
    shot("02-custom-list")
    custom.click()
    state(False, False)
    # The keyboard: Tab to a pill, Enter and Space toggle it.
    shop.focus()
    page.keyboard.press("Enter")
    state(True, False)
    page.keyboard.press("Tab")
    expect(custom).to_be_focused()
    page.keyboard.press("Space")
    state(False, True)
    page.keyboard.press("Space")
    state(False, False)
    page.keyboard.press("Enter")
    state(False, True)

    # Edit tiers: the Vendo dialog, on its Rate tiers tab.
    edit = custom_list.locator(f'li[data-vendo="{inputs["custom_id"]}"]').get_by_role("link", name="Edit tiers", exact=True)
    expect(edit).to_have_attribute("href", f'/admin/vendos?vendo={inputs["custom_id"]}&tab=rates')
    edit.click()
    page.wait_for_url(f'**/admin/vendos?vendo={inputs["custom_id"]}*')
    dialog = page.locator("dialog.vendo-editor")
    expect(dialog).to_be_visible()
    rates = dialog.get_by_role("tab", name="Rate tiers", exact=True)
    expect(rates).to_have_attribute("aria-selected", "true")
    expect(dialog.get_by_role("tabpanel", name="Rate tiers", exact=True)).to_be_visible()
    expect(dialog.get_by_role("tabpanel", name="Details", exact=True)).to_be_hidden()
    shot("03-edit-tiers-opens-rate-tiers-tab")

    # Open: the follower's details, no dialog.
    page.goto(device.portal + "/admin/rates")
    shop.click()
    link = shop_list.locator(f'li[data-vendo="{inputs["follower_id"]}"]').get_by_role("link", name="Open", exact=True)
    expect(link).to_have_attribute("href", f'/admin/vendos?vendo={inputs["follower_id"]}')
    link.click()
    page.wait_for_url(f'**/admin/vendos?vendo={inputs["follower_id"]}')
    expect(page.locator(f'[data-select="{inputs["follower_id"]}"]')).to_have_attribute("aria-current", "true")
    expect(page.locator("[data-detail-slot] .vdetail")).to_be_visible()
    expect(page.locator("dialog.vendo-editor")).to_have_count(0)
    shot("04-open-shows-details")
    return {"pills_toggle": True, "keyboard": True, "edit_tiers_tab": "rates"}


def vouchers_mobile(device: Device, inputs: dict) -> dict:
    """Voucher Batches and code matches remain usable on a touch phone."""
    page = device.page
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    page.goto(device.portal + "/admin/vouchers")

    def in_viewport(locator):
        box = locator.bounding_box()
        assert box and box["x"] >= 0 and box["y"] >= 0, box
        assert box["x"] + box["width"] <= 390 and box["y"] + box["height"] <= 844, box
        return box

    generate = page.get_by_role("button", name="Generate", exact=True)

    def check(row):
        expect(row).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for label in ("Seller", "Price"):
            cell = row.locator(f'[data-label="{label}"]')
            expect(cell).to_be_visible()
            assert cell.inner_text().strip()
            assert cell.evaluate("el => getComputedStyle(el, '::before').content") == f'"{label}"'
        full = in_viewport(generate)["width"]
        for control in (page.get_by_role("searchbox", name="Search Voucher codes"),
                        page.get_by_role("button", name="Search", exact=True)):
            assert abs(control.bounding_box()["width"] - full) < 1
        # Tapping a card's plain text opens that batch's Print sheet.
        with page.expect_popup() as popup:
            row.locator('[data-label="Time"]').click()
        expect(popup.value).to_have_url(re.compile(r"/admin/vouchers\?batch="))
        popup.value.close()

    check(page.locator(".voucher-batch-row").first)
    for action in (page.get_by_role("link", name=re.compile(r"^CSV for Batch ")).first,
                   page.get_by_role("button", name=re.compile(r"^Delete Batch ")).first):
        expect(action).to_be_visible()
        box = action.bounding_box()
        assert box["height"] >= 44 and box["width"] >= 44, box
    page.screenshot(path=f"{device.out}/01-mobile-batches.png", full_page=True)
    device.screenshots.append("01-mobile-batches")
    generate.click()
    dialog = page.get_by_role("dialog", name="Generate Voucher Batch")
    expect(dialog).to_be_visible()
    in_viewport(dialog)
    assert dialog.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    submit = dialog.get_by_role("button", name="Generate Voucher Batch", exact=True)
    submit.scroll_into_view_if_needed()
    in_viewport(submit)
    dialog.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("searchbox", name="Search Voucher codes").fill("Q115")
    with page.expect_navigation():
        page.get_by_role("button", name="Search", exact=True).click()
    expect(page.locator(".voucher-search-row")).to_have_count(50)
    check(page.locator(".voucher-search-row").first)
    page.screenshot(path=f"{device.out}/02-mobile-search.png", full_page=True)
    device.screenshots.append("02-mobile-search")
    return {"viewport": "390x844", "touch": True, "search_matches": 50,
            "no_horizontal_overflow": True, "labelled_price_seller": True, "actions_min_height": 44}


def voucher_print_sheet(device: Device, inputs: dict) -> dict:
    page = device.page
    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    device.context.add_init_script("document.addEventListener('voucher:rendered', e => { window.renderedBatch = e.detail; });")
    loads = []
    page.on("request", lambda request: loads.append(request.url))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def check():
        cards = page.locator('[data-voucher="card"]')
        expect(cards).to_have_count(len(inputs["rows"]))
        assert page.evaluate("window.renderedBatch.vouchers.length") == len(inputs["rows"])
        for row in inputs["rows"]:
            card = cards.filter(has=page.locator('[data-voucher="code"]', has_text=row["code"]))
            expect(card).to_have_count(1)
            expect(card.locator('[data-voucher="code"]')).to_have_text(row["code"])
            expect(card.locator('[data-voucher="qr"] svg')).to_have_count(1)
            expect(card.locator('[data-voucher="shop"]')).to_have_text("OpenPiso WiFi")
            assert card.locator('[data-voucher="time"]').inner_text() == "10 minutes"
            assert card.locator('[data-voucher="validity"]').inner_text() == "3 hours"
            assert card.locator('[data-voucher="pause-limit"]').inner_text() == "7"
            assert card.locator('[data-voucher="pesos"]').inner_text() == "₱5"
            expect(card.locator('[data-voucher="seller"]')).to_have_text("Shop")
            created = card.locator('[data-voucher="created"]').inner_text()
            assert re.fullmatch(r"[A-Z][a-z]{2} [1-9][0-9]?, [0-9]{4} [0-9]{2}:[0-9]{2}", created), created
            datetime.datetime.strptime(created, "%b %d, %Y %H:%M")
            expect(card.locator('[data-voucher="batch"]')).to_have_text(
                page.evaluate("window.renderedBatch.batch.batch"))
            assert card.get_attribute("data-used") == ("" if row["redeemed_at"] else None)
            expect(card.locator('[data-voucher="used"]')).to_have_text("USED" if row["redeemed_at"] else "")
        return cards

    url = device.portal + inputs["path"]
    page.goto(url)
    cards = check()
    assert loads == [url], loads
    assert not errors, errors
    page.evaluate("window.print = () => { window.printClicked = true; };")
    page.get_by_role("button", name="Print", exact=True).click()
    assert page.evaluate("window.printClicked")
    assert cards.first.evaluate("el => el.style.getPropertyValue('--strip')") == "#2563eb"
    page.emulate_media(media="print")
    assert cards.first.evaluate("el => getComputedStyle(el).breakInside") == "avoid"
    assert cards.first.bounding_box()["width"] < 240  # three fixed cards fit A4
    page.screenshot(path=f"{device.out}/01-redeemed-reprint.png", full_page=True)
    device.screenshots.append("01-redeemed-reprint")
    page.emulate_media(media="screen")
    page.set_viewport_size({"width": 375, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    return {"cards": len(inputs["rows"]), "rendered_event": True, "loads": loads}


def voucher_template_editor(device: Device, inputs: dict) -> dict:
    """#118: real draft, preview and publish, then Revert and Reset."""
    page = device.page
    page.goto(device.portal + "/admin/login")
    page.get_by_label("Password").fill(inputs["password"])
    with page.expect_navigation():
        page.get_by_role("button", name="Log in").click()
    editor = device.portal + "/admin/voucher-template"
    sheet = device.portal + inputs["path"]
    page.goto(editor)
    original = page.locator('[name="voucher_html"]').input_value()
    original_shop = page.get_by_label("Shop name", exact=True).input_value()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    device.context.add_init_script("document.addEventListener('voucher:rendered', e => { window.renderedBatch = e.detail; });")

    def submit(name):
        with page.expect_navigation():
            page.get_by_role("button", name=name, exact=True).click()

    def cards(count, used):
        expect(page.locator('[data-voucher="card"]')).to_have_count(count)
        expect(page.locator('[data-voucher="card"][data-used]')).to_have_count(used)
        assert page.evaluate("window.renderedBatch.vouchers.length") == count

    try:
        page.locator('[name="voucher_html"]').fill('<img src="https://example.com/a">')
        page.locator('[name="voucher_css"]').fill('body { background: url("//example.com/a"); }')
        submit("Save draft")
        for reason in ("card", "code", "voucher.html", "voucher.css"):
            expect(page.locator(".alert")).to_contain_text(reason)
        page.goto(editor)
        custom = original.replace("</body>", '<p id="custom-template">Custom template</p><script>window.htmlScriptRan = true;</script></body>')
        page.locator('[name="voucher_html"]').fill(custom)
        page.locator('[name="custom_js"]').fill("throw new Error('voucher custom.js broke');")
        submit("Save draft")
        expect(page.get_by_text("Draft saved", exact=False)).to_be_visible()
        page.goto(sheet)
        cards(len(inputs["rows"]), sum(bool(row["redeemed_at"]) for row in inputs["rows"]))
        expect(page.locator("#custom-template")).to_have_count(0)
        page.goto(editor + "/preview")
        cards(6, 1)
        expect(page.locator("#custom-template")).to_be_visible()
        assert not page.evaluate("Boolean(window.htmlScriptRan)")
        page.screenshot(path=f"{device.out}/01-draft-preview.png", full_page=True)
        device.screenshots.append("01-draft-preview")
        page.goto(editor)
        submit("Publish draft")
        page.get_by_label("Shop name", exact=True).fill("E2E Voucher Shop")
        submit("Save Shop name")
        errors.clear()
        page.goto(sheet)
        cards(len(inputs["rows"]), sum(bool(row["redeemed_at"]) for row in inputs["rows"]))
        expect(page.locator("#custom-template")).to_be_visible()
        assert errors == ["voucher custom.js broke"], errors
        assert not page.evaluate("Boolean(window.htmlScriptRan)")
        expect(page.locator('[data-voucher="shop"]').first).to_have_text("E2E Voucher Shop")
        assert sorted(page.locator('[data-voucher="code"]').all_text_contents()) == sorted(row["code"] for row in inputs["rows"])
        page.screenshot(path=f"{device.out}/02-published-throwing-script.png", full_page=True)
        device.screenshots.append("02-published-throwing-script")
        page.goto(editor)
        submit("Revert to previous")
        errors.clear()
        page.goto(sheet)
        cards(len(inputs["rows"]), sum(bool(row["redeemed_at"]) for row in inputs["rows"]))
        expect(page.locator("#custom-template")).to_have_count(0)
        assert not errors, errors
        page.goto(editor)
        submit("Revert to previous")
        submit("Reset to default")
        errors.clear()
        page.goto(sheet)
        cards(len(inputs["rows"]), sum(bool(row["redeemed_at"]) for row in inputs["rows"]))
        expect(page.locator("#custom-template")).to_have_count(0)
        assert not errors, errors
        page.screenshot(path=f"{device.out}/03-reset-default.png", full_page=True)
        device.screenshots.append("03-reset-default")
    finally:
        page.goto(editor)
        page.get_by_label("Shop name", exact=True).fill(original_shop)
        submit("Save Shop name")
    return {"preview_cards": 6, "preview_used": 1, "throwing_custom_js": True,
            "draft_isolated": True, "reverted": True, "reset": True, "shop_marker": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portal", required=True)
    parser.add_argument("--phase", choices=("first", "rebind", "out", "custom", "preview", "upload", "branded", "sales", "signin", "vendos", "vendo-rates", "shop-vendo-list", "shop-pills", "vouchers-mobile", "voucher-print-sheet", "voucher-template-editor"), required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    inputs = json.loads(sys.stdin.readline())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # The Operator works on a laptop; customers on a phone.
        desktop = args.phase in ("preview", "sales", "signin", "vendos", "vendo-rates", "shop-vendo-list", "shop-pills", "voucher-print-sheet", "voucher-template-editor")
        profile = {"viewport": {"width": 1280, "height": 900}, "locale": "de-DE"} if desktop else playwright.devices[PROFILE]
        context = browser.new_context(**profile)
        context.add_init_script(CSP_LISTENER)
        page = context.new_page()
        page.set_default_timeout(20000)
        device = Device(page, context, args.portal, args.out)
        result = {"first": first, "rebind": rebind, "out": out, "custom": custom, "preview": preview,
                  "upload": upload, "branded": branded, "sales": sales, "signin": signin,
                  "vendos": vendos, "vendo-rates": vendo_rates, "shop-vendo-list": shop_vendo_list, "shop-pills": shop_pills, "vouchers-mobile": vouchers_mobile, "voucher-print-sheet": voucher_print_sheet, "voucher-template-editor": voucher_template_editor}[args.phase](device, inputs)
        assert not device.violations, device.violations
        result.update(browser=f"chromium {browser.version}", device_profile="desktop" if desktop else PROFILE,
                      screenshots=device.screenshots, csp_violations=device.violations)
        browser.close()
    print(json.dumps({"result": result}), flush=True)


if __name__ == "__main__":
    main()
