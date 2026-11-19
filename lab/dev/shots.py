"""Customer-phone flow, run inside the Playwright container by `dev.py shots`.

One Playwright device profile walks the real HotSpot and the dev Controller: redirect to the
portal, open a Coin Window, insert coins with an in-process SimulatedVendo, press Done and reach
the internet. Every step is asserted and screenshotted into --out; result.json is the verdict.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from simulated_vendo import SimulatedVendo  # noqa: E402

START_URL = "http://neverssl.com/"  # any plain http URL: the HotSpot answers with its redirect


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--controller", required=True, help="portal origin reachable through the walled garden")
    parser.add_argument("--vendo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", default=START_URL, help="plain http URL the Device opens first")
    parser.add_argument("--online-text", help="text the start URL serves once online (default: the NeverSSL title)")
    args = parser.parse_args()

    vendo = json.loads(args.vendo.read_text())
    steps: list[dict] = []
    result = {"result": "pass", "steps": steps}
    with sync_playwright() as playwright:
        browser = page = None
        began = time.monotonic()

        def shot(name: str) -> None:
            file = f"{len(steps) + 1:02d}-{name}.png"
            page.screenshot(path=args.out / file, full_page=True)
            steps.append({"step": name, "screenshot": file, "seconds": round(time.monotonic() - began, 1)})

        try:
            device = playwright.devices[args.device]
            browser = getattr(playwright, device["default_browser_type"]).launch()
            page = browser.new_context(**device).new_page()
            page.set_default_timeout(20000)
            result["browser"] = f"{device['default_browser_type']} {browser.version}"

            # 1. Fresh Device, plain http URL: the HotSpot redirects to the Controller portal.
            page.goto(args.start)
            page.wait_for_url(re.compile(re.escape(args.controller) + r"/portal"))
            # "new", not "error": the Controller found this Device by its own IP and MAC.
            expect(page.locator("html")).to_have_attribute("data-portal-state", "new")
            expect(page.get_by_role("heading", name="OpenPiso WiFi")).to_be_visible()
            shot("portal-via-hotspot-redirect")

            # 2. Open a Coin Window on the dev Vendo (one Vendo serves this network: no chooser).
            page.locator('[data-action="insert-coin"]:visible').first.click()
            expect(page.locator("html")).to_have_attribute("data-portal-state", "coin")
            window = page.evaluate("fetch('/portal/api/state').then(response => response.json())")["coin_window"]
            shot("coin-window-open")

            # 3. Insert coins: the Vendo (this process, in the Controller's walled garden) reports them.
            board = SimulatedVendo(args.controller, vendo["id"], bytes.fromhex(vendo["secret"]), vendo["epoch"])
            board.heartbeat()
            assert board.window_id == window["id"], f"Vendo got window {board.window_id}, portal shows {window['id']}"
            board.insert(5)
            board.heartbeat()
            expect(page.locator('[data-portal="coins"]')).to_have_text("₱5")  # the portal refreshes itself
            shot("coins-counted")

            # 4. Done: the Vendo sends its final total, the Controller credits time and the Device goes online.
            with page.expect_response("**/portal/api/coins/done"):
                page.locator('[data-action="coins-done"]:visible').first.click()
            board.heartbeat()
            assert board.closing, "Controller did not tell the Vendo to close"
            time.sleep(0.3)  # the board's window gap before its final total
            board.post(board.frame(5))
            expect(page.locator("html")).to_have_attribute("data-portal-state", "online", timeout=40000)
            bought = page.evaluate("fetch('/portal/api/state').then(response => response.json())")["time_credit"]["bought"]
            assert bought > 0, f"Time Credit was not credited after Done: bought {bought}"
            shot("time-credited")
            for attempt in range(15):  # the portal's own refresh script may cancel a navigation
                try:
                    page.goto(args.start)
                    if args.online_text is None:
                        expect(page).to_have_title(re.compile("NeverSSL"))
                    else:
                        expect(page.locator("body")).to_contain_text(args.online_text)
                    break
                except Exception:
                    if attempt == 14:
                        raise
                    time.sleep(2)
            shot("online")
        except Exception as error:
            result.update(result="fail", error=f"{type(error).__name__}: {str(error)[:600]}")
            if page is not None:
                try:
                    shot("failure")
                except Exception:
                    pass
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    (args.out / "result.json").write_text(json.dumps(result, indent=2))
    print(f"{args.device}: {result['result']}")
    sys.exit(result["result"] != "pass")


main()
