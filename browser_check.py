"""
browser_check.py — Runs the real Flask server as a subprocess, then drives
the actual rendered page with a real Chromium instance via Playwright:
clicks buttons, checks for JS console errors, and takes a screenshot. This
is the only way to genuinely validate the client-side JS rather than just
checking that Jinja templates render without throwing.
"""
import subprocess
import time
import sys
import os

from playwright.sync_api import sync_playwright

PORT = 5050
BASE = f"http://127.0.0.1:{PORT}"


def run_check(product_key: str, screenshot_path: str, actions):
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd="/home/claude/lcr-sim",
        env={**os.environ, "FLASK_RUN_PORT": str(PORT)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # app.py hardcodes port 5000 currently; patch via env not supported yet,
    # so this helper assumes app.py listens on 5000 and we proxy through that.
    time.sleep(1.5)

    console_errors = []
    page_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            page.goto(f"http://127.0.0.1:5000/meter/{product_key}", wait_until="load", timeout=10000)
            page.wait_for_timeout(800)  # let the first poll cycle land and render

            for action in actions:
                action(page)
                time.sleep(0.25)

            page.screenshot(path=screenshot_path, full_page=True)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return console_errors, page_errors


if __name__ == "__main__":
    product = sys.argv[1] if len(sys.argv) > 1 else "lcr2"
    shot = sys.argv[2] if len(sys.argv) > 2 else f"/home/claude/lcr-sim/screenshot_{product}.png"

    if product == "lcr2":
        def actions(page):
            page.click("#runBtn")
            page.wait_for_timeout(200)
            page.fill("#flowRate", "300")
            page.dispatch_event("#flowRate", "change")
            page.wait_for_timeout(1500)  # let some real delivery accumulate
            page.click("#stopBtn")
            page.wait_for_timeout(200)
    elif product == "lcr600":
        def actions(page):
            page.click('[data-screen="POS_TAX"]')
            page.wait_for_timeout(150)
            page.fill("#priceInput", "3.459")
            page.click("#setPriceBtn")
            page.wait_for_timeout(150)
            page.select_option("#taxType", "PERCENT")
            page.fill("#taxValue", "8")
            page.click("#setTaxLineBtn")
            page.wait_for_timeout(150)
            page.click("#runBtn")
            page.fill("#flowRate", "200")
            page.dispatch_event("#flowRate", "change")
            page.wait_for_timeout(1500)
            page.click("#stopBtn")
            page.wait_for_timeout(300)
            # The printer popup auto-opens here (a real delivery ticket was
            # just produced) -- dismiss it before continuing, exactly like a
            # real user clicking the X, since the modal correctly blocks
            # clicks to anything behind it while open.
            if page.locator("#printerPopupBackdrop.open").count() > 0:
                page.click("#printerPopupClose")
                page.wait_for_timeout(150)
            page.click("#buildTicketBtn")
            page.wait_for_timeout(200)
    elif product == "lcriq":
        def actions(page):
            # Original View's softkeys are rendered fresh each poll with
            # data-idx attributes; index 4 is "Start" while idle (see the
            # template's renderSoftkeys comment for why this isn't a fixed id).
            page.wait_for_selector('.ov-softkey[data-idx="4"]:not([disabled])', timeout=5000)
            page.click('.ov-softkey[data-idx="4"]')
            page.fill("#flowRate", "150")
            page.dispatch_event("#flowRate", "change")
            page.wait_for_timeout(800)  # let the valve ramp partway
            page.click("#btOnBtn")
            page.wait_for_timeout(150)
            page.click("#btScanBtn")
            page.wait_for_timeout(150)
            page.click("#btConnectPrinterBtn")
            page.wait_for_timeout(150)
            page.fill("#tankMa", "16")
            page.click("#setTankBtn")
            page.wait_for_timeout(300)
            page.click('.ov-softkey[data-idx="1"]')  # "Stop" while running
            page.wait_for_timeout(300)
    else:
        def actions(page):
            pass

    errs, perrs = run_check(product, shot, actions=actions(None) if False else [actions])
    print("CONSOLE ERRORS:", errs)
    print("PAGE ERRORS:", perrs)
    print("Screenshot saved to:", shot)
