#!/usr/bin/env python3
"""Open an arbitrary URL in CloakBrowser through a proxy.

Standalone helper (separate from the Google auto-login flow). Launches a
visible stealth Chromium routed through a host:port:user:pass proxy, navigates
to a URL, prints the exit IP it actually used, and stays open until Enter.

Usage:
  python open_url.py https://dola.com --proxy HOST:PORT:USER:PASS
  python open_url.py https://dola.com               # uses first line of proxies.txt
  python open_url.py https://dola.com --headless
"""

import argparse
import asyncio
import sys

from proxies import load_proxies, parse_proxy


async def open_url(url, proxy, headless=False, hold=3600):
    from cloakbrowser import launch_async

    if proxy:
        print(f"[i] Proxy: {proxy['server']} (user {proxy['username']})", flush=True)
    else:
        print("[i] Proxy: none (direct connection)", flush=True)

    browser = await launch_async(headless=headless, humanize=True)
    ctx = await browser.new_context(locale="en-US", proxy=proxy)
    page = await ctx.new_page()
    page.set_default_timeout(60000)

    # Confirm the browser context really exits through the proxy IP.
    try:
        await page.goto("https://api.ipify.org", wait_until="domcontentloaded", timeout=30000)
        exit_ip = (await page.content()).strip()
        # strip HTML wrapper if any
        import re
        m = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", exit_ip)
        print(f"[OK] Browser exit IP: {m.group(0) if m else exit_ip}", flush=True)
    except Exception as e:
        print(f"[..] Could not read exit IP: {e}", flush=True)

    print(f"[i] Navigating to {url} ...", flush=True)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"[OK] Loaded: {page.url}", flush=True)
        try:
            print(f"[i] Title: {await page.title()}", flush=True)
        except Exception:
            pass
    except Exception as e:
        print(f"[XX] Navigation error: {e}", flush=True)

    if not headless:
        print(f"[>] Browser is open. Holding for {hold}s "
              f"(close the window or kill this process to stop)...", flush=True)
        try:
            await asyncio.sleep(hold)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    try:
        await browser.close()
    except Exception:
        pass


def resolve_proxy(arg):
    if arg:
        px = parse_proxy(arg)
        if not px:
            print(f"[XX] Bad --proxy (need host:port:user:pass): {arg}", file=sys.stderr)
            sys.exit(1)
        return px
    proxies = load_proxies()
    return proxies[0] if proxies else None


def main():
    p = argparse.ArgumentParser(description="Open a URL in CloakBrowser via a proxy")
    p.add_argument("url", help="URL to open")
    p.add_argument("--proxy", help="host:port:user:pass (default: first line of proxies.txt)")
    p.add_argument("--no-proxy", action="store_true", help="direct connection, ignore proxies")
    p.add_argument("--headless", action="store_true", help="run headless")
    p.add_argument("--hold", type=int, default=3600,
                   help="seconds to keep a visible window open (default 3600)")
    args = p.parse_args()

    proxy = None if args.no_proxy else resolve_proxy(args.proxy)
    asyncio.run(open_url(args.url, proxy, headless=args.headless, hold=args.hold))


if __name__ == "__main__":
    main()
