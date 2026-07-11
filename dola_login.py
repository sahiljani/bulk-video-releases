#!/usr/bin/env python3
"""Drive dola.com's login through a proxy in CloakBrowser.

Opens dola.com, clicks the "Log In" button, then clicks "Continue with Google",
and reports where the Google OAuth flow lands (redirect or popup). Leaves the
window open so the Google sign-in can be completed manually or driven further.

Usage:
  python dola_login.py --proxy HOST:PORT:USER:PASS
  python dola_login.py --headless
"""

import argparse
import asyncio
import sys

from proxies import load_proxies, parse_proxy
from autologin import _handle_google_login, load_accounts


async def click_by_text(page, texts, tags="button, a, [role=button], input[type=submit]"):
    """Click the first visible control whose OWN text matches one of `texts`.

    Matches only leaf controls (buttons/links), preferring an exact text match
    and otherwise a short element that merely contains the phrase — this avoids
    clicking large container elements whose textContent happens to include the
    word.
    """
    return await page.evaluate(
        """([texts, tags]) => {
            const want = texts.map(t => t.toLowerCase());
            const cands = [...document.querySelectorAll(tags)].filter(el => el.offsetParent !== null);
            const textOf = el => (el.textContent || el.value || '').trim().toLowerCase().replace(/\\s+/g,' ');
            // Pass 1: exact match.
            for (const el of cands) {
                const t = textOf(el);
                if (want.some(w => t === w)) { el.click(); return t.slice(0, 40); }
            }
            // Pass 2: short element containing the phrase.
            for (const el of cands) {
                const t = textOf(el);
                if (t.length <= 40 && want.some(w => t.includes(w))) { el.click(); return t.slice(0, 40); }
            }
            return null;
        }""",
        [texts, tags],
    )


async def run(proxy, headless=False, hold=3600):
    from cloakbrowser import launch_persistent_context_async

    print(f"[i] Proxy: {proxy['server'] if proxy else 'none'}", flush=True)
    ctx = await launch_persistent_context_async(
        user_data_dir="/home/azureuser/bulk-Video-generation/app/sessions/ypatel42011a_gmail_com",
        headless=headless, 
        humanize=True,
        locale="en-US", 
        proxy=proxy,
        viewport={"width": 1280, "height": 900}
    )
    page = await ctx.new_page()
    page.set_default_timeout(45000)

    # Track any OAuth popup Google may open.
    popups = []
    ctx.on("page", lambda pg: popups.append(pg))

    await page.goto("https://dola.com", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(7)
    print(f"[OK] Loaded {page.url}", flush=True)

    # Dismiss the cookie banner if present (its "OK" button can intercept clicks).
    await page.evaluate(
        "() => { for (const el of document.querySelectorAll('button'))"
        " { if ((el.textContent||'').trim().toLowerCase()==='ok') { el.click(); return; } } }")
    await asyncio.sleep(1)

    async def modal_open():
        return await page.evaluate("() => document.body.innerText.includes('Unlock More Features')")

    # Step 1: ensure the login modal is open. It sometimes auto-appears a few
    # seconds after load and sometimes needs the "Log In" button — retry either.
    for attempt in range(8):
        if await modal_open():
            break
        clicked = await click_by_text(page, ["log in", "login", "sign in"])
        if clicked:
            print(f"[i] Clicked login control: {clicked!r}", flush=True)
        await asyncio.sleep(2)
    print(f"[i] Login modal open: {await modal_open()}", flush=True)

    # Step 2: click the Google button. dola A/B-tests two modal variants:
    #   A) three icon circles — Google is a `div[class*=button-]` with an <img>
    #   B) one full-width "Continue with Google" text button
    # Both render the same Google logo <img class="size-24">, so target text
    # first (variant B) and fall back to the logo img (variant A).
    google = page.locator(
        'button:has-text("Continue with Google"), '
        '[class*="button-"]:has-text("Continue with Google"), '
        'button:has(img.size-24), '
        '[class*="button-"]:has(img.size-24)'
    ).first
    try:
        await google.wait_for(state="visible", timeout=10000)
        print(f"[i] Google button found (count={await google.count()}) — clicking", flush=True)
    except Exception:
        print("[XX] Google button not found in modal", flush=True)

    # The click may either open a popup/new tab OR redirect the main tab. Fire
    # the click and then poll both for accounts.google.com for up to 25s.
    try:
        await google.click()
    except Exception as e:
        print(f"[XX] Google click failed: {type(e).__name__}: {e}", flush=True)

    def google_page():
        for pg in [page] + popups:
            try:
                if "accounts.google.com" in (pg.url or ""):
                    return pg
            except Exception:
                pass
        return None

    gpg = None
    for _ in range(25):
        gpg = google_page()
        if gpg:
            break
        await asyncio.sleep(1)

    print(f"[i] Main tab URL: {page.url}", flush=True)
    for i, pg in enumerate([p for p in popups if p is not page]):
        try:
            print(f"[i] Extra window {i}: {pg.url}", flush=True)
        except Exception:
            pass

    on_google = gpg is not None
    if on_google:
        try:
            await gpg.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        print(f"[OK] Reached Google sign-in: {gpg.url}", flush=True)
        
        # ADDED: read accounts and automate login
        accounts = load_accounts("accounts.txt")
        if accounts:
            email, password = accounts[0]
            print(f"[i] Automating login for {email}", flush=True)
            await _handle_google_login(gpg, email, password)
        else:
            print("[XX] No accounts found in accounts.txt!", flush=True)
    else:
        print("[..] Reached Google sign-in: False", flush=True)

    # Dump the post-click DOM + any error toast so we can see what happened even
    # if screenshots hang (a headless quirk here).
    import os
    shot_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        body = await page.evaluate("()=>document.body.innerText.slice(0,1500)")
        print("[i] --- main page visible text after click ---", flush=True)
        print(body, flush=True)
        print("[i] --- end text ---", flush=True)
    except Exception:
        pass
    for i, pg in enumerate([page] + [p for p in popups if p is not page]):
        try:
            path = os.path.join(shot_dir, f"dola_flow_{i}.png")
            await asyncio.wait_for(pg.screenshot(path=path), timeout=10)
            print(f"[i] Screenshot page {i}: {pg.url[:70]} -> {path}", flush=True)
        except Exception as e:
            print(f"[..] Screenshot page {i} failed: {type(e).__name__}", flush=True)

    if not headless:
        print(f"[>] Holding window open {hold}s...", flush=True)
        try:
            await asyncio.sleep(hold)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    try:
        await ctx.close()
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser(description="Drive dola.com Google login via CloakBrowser")
    p.add_argument("--proxy", help="host:port:user:pass (default: first line of proxies.txt)")
    p.add_argument("--no-proxy", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--hold", type=int, default=3600)
    args = p.parse_args()

    if args.no_proxy:
        proxy = None
    elif args.proxy:
        proxy = parse_proxy(args.proxy)
    else:
        pl = load_proxies()
        proxy = pl[0] if pl else None

    asyncio.run(run(proxy, headless=args.headless, hold=args.hold))


if __name__ == "__main__":
    main()
