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
            page.goto(START_URL)
            page.wait_for_url(re.compile(re.escape(args.controller) + r"/portal"))
            expect(page.get_by_role("heading", name="OpenPiso WiFi")).to_be_visible()
            expect(page.get_by_text("Controller is ready")).to_be_visible()
            # The Controller must see the Device's own IP (return route), not the Router's.
            expect(page.get_by_role("alert")).to_have_count(0)
            shot("portal-via-hotspot-redirect")

            # 2. Open a Coin Window on the dev Vendo.
            page.get_by_role("button", name="Insert coin").click()
            heading = page.get_by_role("heading", name=re.compile(r"Coin Window \d+"))
            expect(heading).to_be_visible()
            window_id = int(re.search(r"\d+", heading.inner_text()).group())
            expect(page.get_by_text(f"Vendo {vendo['id']} · open")).to_be_visible()
            shot("coin-window-open")

            # 3. Insert coins: the Vendo (this process, in the Controller's walled garden) reports them.
            board = SimulatedVendo(args.controller, vendo["id"], bytes.fromhex(vendo["secret"]), vendo["epoch"])
            board.heartbeat()
            assert board.window_id == window_id, f"Vendo got window {board.window_id}, portal shows {window_id}"
            board.insert(5)
            board.heartbeat()
            expect(page.get_by_text("Coins counted: 5 pesos")).to_be_visible()  # the portal refreshes itself
            shot("coins-counted")

            # 4. Done: the Vendo sends its final total, the Controller credits time and the Device goes online.
            page.get_by_role("button", name="Done").click()
            expect(page.get_by_text("Closing Coin Window")).to_be_visible()
            board.heartbeat()
            assert board.closing, "Controller did not tell the Vendo to close"
            time.sleep(0.3)  # the board's window gap before its final total
            board.post(board.frame(5))
            deadline = time.monotonic() + 40
            while True:
                page.goto(args.controller + "/portal")
                if page.get_by_text(re.compile(r"Time bought: [1-9]\d* seconds")).count():
                    break
                assert time.monotonic() < deadline, "Time Credit was not credited after Done"
                time.sleep(1)
            expect(page.get_by_text("Coins counted: 5 pesos")).to_be_visible()
            shot("time-credited")
            for attempt in range(15):  # the portal's own refresh script may cancel a navigation
                try:
                    page.goto(START_URL)
                    expect(page).to_have_title(re.compile("NeverSSL"))
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
