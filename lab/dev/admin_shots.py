"""Admin panel screenshots, run inside the Playwright container by `dev.py admin-shots`.

Usage: python -I admin_shots.py --populated '<adminshots JSON line>' --empty '<adminshots -empty JSON line>' --out /out
Writes <screen>--<state>--<width>-<theme>.png per state and exits non-zero if any state failed
(page error, HTTP >= 400, missing selector or a redirect to the login page).
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

VIEWPORTS = {1280: (1280, 900), 1920: (1920, 1080), 390: (390, 844)}
POPULATED_SIZES = [(1280, "light"), (1280, "dark"), (1920, "light"), (390, "light"), (390, "dark")]
INTERACTION_SIZES = [(1280, "light"), (390, "dark")]
EMPTY_SIZES = [(1280, "light"), (390, "light")]

# Selectors that mean the page finished building: rows the server renders, plus the parts admin.js builds.
DASHBOARD = "#dashboard"
SALES = "#sales-report .sales-totals"
VENDOS = "[data-vendos] [data-filter=all] .filter-count:not(:empty)"
TIERS = "form[data-tiers] .tier-cols"


def open_generate(page) -> None:
    page.get_by_role("button", name="Generate", exact=True).click()
    page.wait_for_selector("dialog.voucher-dialog[open]")


def open_edit(page) -> None:
    page.locator("[data-vendo-edit]:visible").first.click()
    page.wait_for_selector("dialog.vendo-editor[open] [data-edit-tab=details]")


def focus_filter(page) -> None:
    # Phones keep the filters in a sheet behind "Filter Sales": open it from the keyboard.
    sheet_button = page.get_by_role("button", name="Filter Sales")
    if sheet_button.is_visible():
        sheet_button.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("dialog.sales-sheet[open]")
    else:
        # The table view keeps its filters in a closed <details>; open it without a click.
        page.locator("details.sales-filter-panel").evaluate("panel => { panel.open = true; }")
    # A keyboard move, so :focus-visible draws the ring as it would for a keyboard user.
    select = page.locator(".sales-filters select[name=vendo]:visible")
    select.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    page.wait_for_selector(".sales-filters select[name=vendo]:focus-visible")


def states(populated: dict, empty: dict) -> list:
    """(name, base, cookie, path, ready selector, action, sizes); action runs after ready."""
    vendo = populated["vendos"][0]  # the Online Vendo with its own Rate tiers and Points table
    full, bare = (populated["base"], populated["cookie"]), (empty["base"], empty["cookie"])
    rows = [
        ("dashboard--default", full, "/admin/dashboard", DASHBOARD, None, POPULATED_SIZES),
        ("sales--table", full, "/admin/sales", SALES, None, POPULATED_SIZES),
        ("sales--feed", full, "/admin/sales?view=feed", SALES, None, POPULATED_SIZES),
        ("vendos--default", full, f"/admin/vendos?vendo={vendo}", VENDOS, None, [s for s in POPULATED_SIZES if s[0] != 390]),
        ("vendos--default", full, "/admin/vendos", VENDOS, None, [s for s in POPULATED_SIZES if s[0] == 390]),
        ("vouchers--default", full, "/admin/vouchers", "tr.voucher-batch-row", None, POPULATED_SIZES),
        ("shop--rates", full, "/admin/rates", TIERS + " ~ [data-tier-list] .tier", None, POPULATED_SIZES),
        ("shop--points", full, "/admin/points", TIERS + " ~ [data-tier-list] .tier", None, POPULATED_SIZES),
        ("vouchers--generate-dialog", full, "/admin/vouchers", "tr.voucher-batch-row", open_generate, INTERACTION_SIZES),
        ("vendos--edit-dialog", full, f"/admin/vendos?vendo={vendo}", VENDOS, open_edit, INTERACTION_SIZES),
        ("sales--filter-focus", full, "/admin/sales", SALES, focus_filter, INTERACTION_SIZES),
        ("dashboard--empty", bare, "/admin/dashboard", DASHBOARD, None, EMPTY_SIZES),
        ("sales--empty", bare, "/admin/sales", SALES, None, EMPTY_SIZES),
        ("vendos--empty", bare, "/admin/vendos", "[data-vendos]", None, EMPTY_SIZES),
        ("vouchers--empty", bare, "/admin/vouchers", "dialog[x-ref=generate]", None, EMPTY_SIZES),
        ("shop--rates-empty", bare, "/admin/rates", TIERS, None, EMPTY_SIZES),
    ]
    return [(name, base, cookie, path, ready, action, width, theme)
            for name, (base, cookie), path, ready, action, sizes in rows for width, theme in sizes]


def shoot(browser, out: Path, name, base, cookie, path, ready, action, width, theme) -> str | None:
    """One fresh context per state; returns the failure, or None."""
    w, h = VIEWPORTS[width]
    mobile = width == 390
    context = browser.new_context(viewport={"width": w, "height": h}, color_scheme=theme, is_mobile=mobile,
                                  has_touch=mobile, device_scale_factor=2 if mobile else 1,
                                  reduced_motion="reduce", timezone_id="Asia/Manila", locale="en-PH")
    host = urlparse(base).hostname
    context.add_cookies([{"name": "openpiso_admin", "value": cookie, "domain": host, "path": "/admin", "httpOnly": True}])
    problems = []
    page = context.new_page()
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
    page.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    try:
        page.goto(base + path, wait_until="networkidle")
        if urlparse(page.url).path == "/admin/login":
            return "redirected to /admin/login"
        page.wait_for_selector(ready, state="attached", timeout=10_000)
        if action:
            action(page)
        # A modal's backdrop covers only the viewport, so dialog states are viewport shots.
        page.screenshot(path=str(out / f"{name}--{width}-{theme}.png"), full_page=not page.locator("dialog[open]").count(),
                        animations="disabled")
    except Exception as error:  # Playwright timeouts and assertion failures: report, keep going
        problems.append(str(error).splitlines()[0])
    finally:
        context.close()
    return "; ".join(problems) or None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--populated", required=True)
    parser.add_argument("--empty", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    failed = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for state in states(json.loads(args.populated), json.loads(args.empty)):
            problem = shoot(browser, args.out, *state)
            label = f"{state[0]}--{state[6]}-{state[7]}"
            print(f"{'FAIL' if problem else 'ok  '} {label}" + (f": {problem}" if problem else ""), flush=True)
            failed += bool(problem)
        browser.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
