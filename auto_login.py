#!/usr/bin/env python3
"""
Plain single-account login test for YOUR OWN OAuth app + YOUR OWN Google account.

Scope (deliberate):
  - Standard Playwright. NO cloakbrowser / stealth / fingerprint spoofing.
  - ONE account. NO accounts.txt batch loop.
  - NO proxy rotation.
  - Visible browser (headless=False) so you can watch / take over if needed.
  - Credentials are prompted at runtime and NEVER written to disk.

This drives the real Google login, which Google actively blocks for automation.
If you hit "Couldn't sign you in / This browser or app may not be secure",
that is Google's bot protection doing its job — complete the step by hand in the
open window, or use the mock-provider harness for a reliable automated test.

Prereqs:
  app/.venv/Scripts/python.exe -m pip install playwright
  app/.venv/Scripts/python.exe -m playwright install chromium

Run (start the Flask app first, in another terminal):
  app/.venv/Scripts/python.exe app/auto_login.py
"""

import getpass
import sys

from playwright.sync_api import sync_playwright

APP_LOGIN_URL = "http://localhost:5000/login"
SUCCESS_URL_FRAGMENT = "localhost:5000"  # your /callback lands back here


def main():
    email = input("Google email (your test account): ").strip()
    if not email:
        print("No email given. Aborting.")
        sys.exit(1)
    # getpass keeps the password off the screen and out of any file.
    password = getpass.getpass("Google password (not stored): ")
    if not password:
        print("No password given. Aborting.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # visible on purpose
        page = browser.new_page()
        page.set_default_timeout(30_000)

        print(f"[1/4] Opening {APP_LOGIN_URL} ...")
        page.goto(APP_LOGIN_URL, wait_until="domcontentloaded")

        # --- Email step ---
        print("[2/4] Entering email ...")
        try:
            page.wait_for_selector("#identifierId", timeout=15_000)
            page.fill("#identifierId", email)
            page.click("#identifierNext")
        except Exception as e:
            print(f"    Could not complete the email step automatically: {e}")
            print("    Finish it by hand in the open window if you like.")

        # --- Password step ---
        print("[3/4] Entering password ...")
        try:
            page.wait_for_selector('input[name="Passwd"]', timeout=15_000)
            page.fill('input[name="Passwd"]', password)
            page.click("#passwordNext")
        except Exception as e:
            print(f"    Could not complete the password step automatically: {e}")
            print("    Google may have shown a bot-check. Complete it by hand.")

        # --- Wait for redirect back to your app's /callback ---
        print("[4/4] Waiting for redirect back to your app ...")
        try:
            page.wait_for_url(f"**{SUCCESS_URL_FRAGMENT}**", timeout=90_000)
            print(f"\n[OK] Landed on: {page.url}")
            if "/callback" in page.url or page.url.rstrip("/").endswith(":5000"):
                print("[OK] OAuth round-trip completed via automation.")
        except Exception:
            print("\n[--] Never made it back to your app within 90s.")
            print("     Most likely Google blocked the automated login (expected).")
            print("     The browser stays open so you can inspect / finish manually.")

        input("\nPress Enter to close the browser ...")
        browser.close()


if __name__ == "__main__":
    main()
