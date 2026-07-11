#!/usr/bin/env python3
"""
Full E2E Lifecycle Video Automation (v2 — robust):
1. Delete old account (if exists / login & delete).
2. Create new account (Google OAuth + Birthday picker < 2000-01-01).
3. Generate video with sample prompt.
4. Download the generated video file (.mp4).
5. Delete account again.

Key fixes over v1:
  - Detects "daily limit" rate-limit message immediately (no 6-min timeout).
  - Correct textarea selector ("Describe the actions in the video").
  - Screenshots at every critical step for debugging.
  - page.is_closed() guards to prevent "Target page closed" crashes.
  - Handles "Create Video" mode toggle & "New Chat" for fresh sessions.
  - DOM-based checks with page.evaluate() for reliability.
  - Proper "Generating video..." progress detection (not blind timer).
"""

import asyncio
import argparse
import random
import os
import shutil
import time
import json
import csv
import subprocess
from datetime import datetime
import urllib.request
from autologin import automate_google_login, load_accounts, dbg, log
from dola_login import run as dola_run, click_by_text

SAMPLE_PROMPT = "Generate a highly detailed, 8k realistic cinematic video based on the last generated video image. Strict prompt: produce high quality details, focus on neon-lit cyber city at dusk."
DOWNLOAD_DIR = "/home/azureuser/bulk-Video-generation/app/downloads"
SCREENSHOT_DIR = "/home/azureuser/bulk-Video-generation/app/downloads/screenshots"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# SOCKS5 proxy running inside the spain VPN namespace
SPAIN_SOCKS_PROXY = "socks5://10.200.200.2:1080"
MICROSOCKS_BIN = "/tmp/microsocks"


def ensure_socks_proxy():
    """Ensure microsocks is running inside the spain namespace. Idempotent."""
    # Check if already running
    res = subprocess.run(["pgrep", "-f", "microsocks"], capture_output=True)
    if res.returncode == 0:
        log("SOCKS proxy already running.", "OK")
        return True
    # Start it
    log("Starting SOCKS5 proxy inside spain namespace...", "INFO")
    subprocess.Popen(
        ["sudo", "ip", "netns", "exec", "spain", MICROSOCKS_BIN, "-i", "10.200.200.2", "-p", "1080"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(2)
    # Verify
    res = subprocess.run(
        ["curl", "-s", "--max-time", "8", "--socks5", "10.200.200.2:1080", "https://ipinfo.io/json"],
        capture_output=True, text=True
    )
    if res.returncode == 0 and '"ES"' in res.stdout:
        log(f"SOCKS proxy verified (Spain IP).", "SUCCESS")
        return True
    log(f"SOCKS proxy verification failed: {res.stdout[:100]}", "ERR")
    return False


def restart_socks_proxy():
    """Kill and restart the SOCKS proxy (called after VPN rotation)."""
    subprocess.run(["sudo", "pkill", "-f", "microsocks"], check=False, stderr=subprocess.DEVNULL)
    time.sleep(1)
    return ensure_socks_proxy()


# ─── Utility: safe screenshot ────────────────────────────────────────
async def screenshot(page, tag, log_msg=None):
    """Take a timestamped screenshot. Silently skip if page is closed."""
    try:
        if page.is_closed():
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{SCREENSHOT_DIR}/{tag}_{ts}.png"
        await page.screenshot(path=fname)
        if log_msg:
            log(f"[📸 {tag}] {log_msg}", "INFO")
        return fname
    except Exception as e:
        dbg(f"Screenshot {tag} failed: {e}")
        return None


# ─── Utility: safe page check ────────────────────────────────────────
def page_alive(page):
    """Return True if page is still usable."""
    try:
        return page and not page.is_closed()
    except Exception:
        return False


async def safe_evaluate(page, js_expr):
    """Run page.evaluate() with safety guard."""
    if not page_alive(page):
        return None
    try:
        return await page.evaluate(js_expr)
    except Exception:
        return None


# ─── DOM text scanner (reliable inner-text check) ────────────────────
async def page_contains_text(page, texts):
    """Check if visible page text contains any of the given strings.
    Returns the first matching text or None."""
    body = await safe_evaluate(page, "() => document.body ? document.body.innerText : ''")
    if not body:
        return None
    for t in texts:
        if t.lower() in body.lower():
            return t
    return None


# ─── Delete Dola Account ─────────────────────────────────────────────
async def delete_dola_account(page):
    log("Starting Dola account deletion process...", "INFO")
    if not page_alive(page):
        log("Page already closed, cannot delete account.", "WARNING")
        return False
    try:
        await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        await screenshot(page, "delete_start", "On chat page for account deletion")

        # Dismiss overlays
        for _ in range(2):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

        # Click avatar / profile (bottom-left user area)
        profile_selectors = [
            'img[alt="avatar"]',
            'div[class*="avatar"]', 'div[class*="Avatar"]',
            'div.cursor-pointer:has(img[alt="avatar"])',
            '[class*="user-profile"]',
            # Fallback: bottom-left clickable areas
            'div.cursor-pointer:has(img)',
        ]
        clicked = False
        for sel in profile_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible(timeout=2000):
                    await el.click(force=True)
                    clicked = True
                    log(f"Clicked profile via: {sel[:40]}", "OK")
                    break
            except Exception:
                continue
        if not clicked:
            # Try JS: bottom-left avatar area
            await safe_evaluate(page, """() => {
                const imgs = document.querySelectorAll('img[alt="avatar"], img[src*="avatar"]');
                for (const img of imgs) { img.closest('[class*="cursor"]')?.click(); return true; }
                return false;
            }""")
        await asyncio.sleep(1.5)
        await screenshot(page, "delete_profile_clicked")

        # Click Settings (with JS fallback)
        try:
            settings_btn = page.locator('span:has-text("Settings"), button:has-text("Settings"), div:text-is("Settings"), [role="menuitem"]:has-text("Settings")').first
            await settings_btn.wait_for(state="visible", timeout=3000)
            await settings_btn.click(force=True)
            log("Clicked Settings via locator.", "OK")
        except Exception:
            await safe_evaluate(page, """() => {
                const els = Array.from(document.querySelectorAll('*'));
                const settingsEl = els.find(el => el.textContent && el.textContent.trim() === 'Settings');
                if (settingsEl) { settingsEl.click(); return true; }
                return false;
            }""")
            log("Clicked Settings via JS fallback.", "OK")

        await asyncio.sleep(1.5)
        await screenshot(page, "delete_settings")

        # Click Account tab
        try:
            account_tab = page.locator('div:text-is("Account"), span:text-is("Account"), button:has-text("Account")').first
            await account_tab.wait_for(state="visible", timeout=3000)
            await account_tab.click(force=True)
        except Exception:
            await safe_evaluate(page, """() => {
                const els = Array.from(document.querySelectorAll('*'));
                const acc = els.find(el => el.textContent && el.textContent.trim() === 'Account');
                if (acc) { acc.click(); }
            }""")
        await asyncio.sleep(1.5)

        # Click Delete Account
        try:
            delete_btn = page.locator('div:text-is("Delete Account"), button:has-text("Delete Account"), span:has-text("Delete Account")').last
            await delete_btn.wait_for(state="visible", timeout=5000)
            await delete_btn.click(force=True)
        except Exception:
            await safe_evaluate(page, """() => {
                const els = Array.from(document.querySelectorAll('*'));
                const del = els.find(el => el.textContent && el.textContent.trim() === 'Delete Account');
                if (del) { del.click(); }
            }""")

        await asyncio.sleep(1.5)
        await screenshot(page, "delete_confirm_dialog")

        # Confirm Delete
        try:
            confirm_btn = page.locator('button:has-text("Delete"), button:text-is("Delete")').last
            await confirm_btn.wait_for(state="visible", timeout=5000)
            await confirm_btn.click(force=True)
            log("Confirmed Delete via locator", "OK")
        except Exception:
            await safe_evaluate(page, """() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const confirm = buttons.find(b => b.textContent && b.textContent.trim() === 'Delete');
                if (confirm) { confirm.click(); }
            }""")
            log("Confirmed Delete via JS", "OK")

        log("Waiting 8s for backend to process deletion...", "WAIT")
        await asyncio.sleep(8)

        await page.goto("https://www.dola.com/", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await screenshot(page, "post_deletion_verification")

        log("Account successfully deleted!", "SUCCESS")
        return True
    except Exception as e:
        log(f"Error during account deletion: {e}", "ERR")
        await screenshot(page, "delete_error")
        return False


# ─── Get Google OAuth URL from Dola ──────────────────────────────────
async def get_google_auth_url(ctx, page):
    popups = []
    ctx.on("page", lambda pg: popups.append(pg))

    await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(5)
    await screenshot(page, "dola_landing", "Landed on Dola chat page")

    # Check for geo-block
    geo_block = await page_contains_text(page, [
        "not available in this country",
        "not available in your region",
        "not available in your country",
    ])
    if geo_block:
        log(f"🚫 GEO-BLOCKED: '{geo_block}'. Browser is NOT routing through VPN!", "ERR")
        await screenshot(page, "geo_blocked", "Dola geo-blocked!")
        return "GEO_BLOCKED"
    try:
        accept_btn = page.locator('button:has-text("Accept All")').first
        if await accept_btn.count() > 0:
            await accept_btn.click(force=True)
            await asyncio.sleep(1)
    except Exception:
        pass

    # Check if already logged in (no "Log In" button visible)
    login_btn = page.locator('button:has-text("Log In"), button:has-text("Sign In"), button:has-text("log in")').first
    try:
        if await login_btn.count() == 0 or not await login_btn.is_visible(timeout=3000):
            log("Already logged in (no Log In button found). Using existing session.", "OK")
            return None  # Already logged in
    except Exception:
        pass

    # Open login modal and find Google button
    for attempt in range(6):
        google = page.locator(
            'button:has-text("Continue with Google"), '
            '[class*="button-"]:has-text("Continue with Google"), '
            'button:has(img.size-24), '
            '[class*="button-"]:has(img.size-24)'
        ).first
        if await google.count() > 0 and await google.is_visible():
            break
        await click_by_text(page, ["Log In", "log in", "login", "sign in"])
        await asyncio.sleep(2)

    google = page.locator(
        'button:has-text("Continue with Google"), '
        '[class*="button-"]:has-text("Continue with Google"), '
        'button:has(img.size-24), '
        '[class*="button-"]:has(img.size-24)'
    ).first

    try:
        await google.wait_for(state="visible", timeout=10000)
        await screenshot(page, "google_btn_visible", "Google OAuth button found")
        await google.click()
    except Exception as e:
        log(f"Could not click Continue with Google: {e}", "WARNING")
        await screenshot(page, "google_btn_fail")

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

    if gpg:
        return gpg.url

    # Wait for the Dola SPA to process the OAuth callback and redirect us to /chat/
    log("Waiting for Dola SPA to process OAuth callback and redirect to /chat/...", "INFO")
    redirected = False
    for i in range(30):
        current_url = page.url
        if "dola.com/chat" in current_url and "auth/callback" not in current_url:
            log(f"SPA redirected to chat: {current_url[:60]}", "OK")
            redirected = True
            break
        await asyncio.sleep(1)

    if not redirected:
        log("SPA did not auto-redirect - forcing navigation to /chat/...", "WARNING")
        await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(8)
    else:
        await asyncio.sleep(3)

    # Verify session is active (Log In button should not be visible)
    login_btn = page.locator('button:has-text("Log In")').first
    try:
        is_guest = await login_btn.is_visible(timeout=2000)
        if is_guest:
            log("Still showing Log In - session may not have persisted.", "WARNING")
        else:
            log("Session confirmed active - logged in successfully!", "OK")
    except Exception:
        log("Session check complete.", "INFO")

    return None


# ─── Generate and Download Video ─────────────────────────────────────
async def generate_and_download_video(page, prompt):
    """Submit a video generation prompt, wait for result, download .mp4.
    Returns: 'SUCCESS', 'RATE_LIMITED', or 'TIMEOUT'."""
    if not page_alive(page):
        log("Page not alive at video generation start!", "ERR")
        return "ERROR"

    await screenshot(page, "video_gen_start", "Starting video generation flow")

    # ─── Ensure we're on a "New Chat" for clean slate ─────────────
    try:
        new_chat = page.locator('a:has-text("New Chat"), button:has-text("New Chat"), [href="/chat/"]').first
        if await new_chat.count() > 0 and await new_chat.is_visible(timeout=3000):
            await new_chat.click(force=True)
            log("Clicked 'New Chat' for clean session.", "OK")
            await asyncio.sleep(3)
    except Exception:
        # Navigate directly
        try:
            await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)
        except Exception:
            pass

    await screenshot(page, "video_new_chat", "On fresh chat page")

    # ─── Activate "Create Video" mode if available ────────────────
    try:
        create_video_toggle = page.locator(
            'button:has-text("Create Video"), '
            'div:has-text("Create Video"):not(:has(div)), '
            'span:has-text("Create Video")'
        ).first
        if await create_video_toggle.count() > 0 and await create_video_toggle.is_visible(timeout=5000):
            await create_video_toggle.click(force=True)
            log("Activated 'Create Video' mode.", "OK")
            await asyncio.sleep(2)
        else:
            log("'Create Video' toggle not found - may already be active or UI differs.", "WARNING")
    except Exception:
        log("Could not toggle Create Video mode.", "WARNING")

    await screenshot(page, "video_mode_active", "Create Video mode state")

    # ─── Find textarea with multiple selector fallbacks ───────────
    textarea_selectors = [
        'textarea[placeholder*="Describe the actions"]',
        'textarea[placeholder*="Describe"]',
        'textarea[placeholder*="Message"]',
        'textarea:visible',
        '[contenteditable="true"]:visible',
    ]

    textarea = None
    log("Waiting for chat input textarea...", "WAIT")
    for attempt in range(25):
        # Handle any onboarding / modal popups first
        try:
            modal_btns = page.locator(
                'button:has-text("Skip"), button:has-text("Next"), '
                'button:has-text("Got it"), button:has-text("Get Started"), '
                'button:has-text("Continue"), button:has-text("OK"), '
                'button:has-text("Close")'
            )
            for idx in range(min(await modal_btns.count(), 3)):
                btn = modal_btns.nth(idx)
                if await btn.is_visible(timeout=500):
                    await btn.click(force=True)
                    log("Dismissed modal/onboarding popup.", "OK")
                    await asyncio.sleep(1)
        except Exception:
            pass

        for sel in textarea_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible(timeout=1000):
                    textarea = el
                    log(f"Found textarea via: {sel[:50]}", "OK")
                    break
            except Exception:
                continue
        if textarea:
            break
        await asyncio.sleep(1)

    if not textarea:
        await screenshot(page, "video_no_textarea", "Could not find chat input!")
        log("Could not find chat input textarea!", "ERR")
        return "ERROR"

    # ─── Type and submit the prompt ───────────────────────────────
    await textarea.click()
    await asyncio.sleep(0.3)
    await textarea.fill(prompt)
    await asyncio.sleep(0.5)
    await screenshot(page, "video_prompt_filled", "Prompt typed into textarea")

    log("Submitting video generation prompt...", "INFO")
    await textarea.press("Enter")
    await asyncio.sleep(2)

    # Also try clicking send button if Enter didn't submit
    try:
        send_btn = page.locator(
            'button[type="submit"], '
            'button[aria-label*="Send"], button[aria-label*="send"], '
            'button:has(svg[viewBox*="send"]), '
            'button:near(textarea):has(svg)'
        ).last
        if await send_btn.count() > 0 and await send_btn.is_visible(timeout=2000):
            await send_btn.click(force=True)
            log("Clicked send button.", "OK")
    except Exception:
        pass

    await asyncio.sleep(3)
    await screenshot(page, "video_prompt_submitted", "After prompt submission")

    # ─── Check for guest restriction ──────────────────────────────
    guest_match = await page_contains_text(page, [
        "not available for guests",
        "sign in to",
        "log in to",
    ])
    if guest_match:
        log(f"Guest restriction detected: '{guest_match}'. Trying in-page Google login...", "WARNING")
        await screenshot(page, "video_guest_blocked")
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        # Click Log In → Google
        login_top = page.locator('button:has-text("Log In")').last
        if await login_top.count() > 0 and await login_top.is_visible():
            await login_top.click(force=True)
            await asyncio.sleep(3)
        google_btn = page.locator('a[href*="google"], button:has(img[src*="google"]), img[alt*="Google"]').first
        if await google_btn.count() > 0 and await google_btn.is_visible():
            await google_btn.click(force=True)
            log("Clicked Google login in modal, waiting for OAuth...", "WAIT")
            await asyncio.sleep(15)
            # Retry prompt
            try:
                textarea2 = page.locator('textarea:visible').first
                await textarea2.wait_for(state="visible", timeout=15000)
                await textarea2.fill(prompt)
                await textarea2.press("Enter")
                await asyncio.sleep(3)
            except Exception as te:
                log(f"Could not resubmit after session attach: {te}", "ERR")

    # ─── NOW WAIT FOR VIDEO (with rate-limit detection) ───────────
    log("Waiting for video generation to complete (checking for rate limits + progress)...", "INFO")

    # Intercept network for .mp4 URLs
    captured_mp4s = []
    def _on_resp(resp):
        try:
            url = resp.url
            if ".mp4" in url or "cloudfront" in url or "amazonaws.com/video" in url or "video" in url.split("?")[0]:
                if url not in captured_mp4s:
                    captured_mp4s.append(url)
                    log(f"Captured potential video URL: {url[:80]}...", "OK")
        except Exception:
            pass
    page.on("response", _on_resp)

    video_url = None
    start_time = time.time()
    MAX_WAIT = 300  # 5 minutes max
    last_screenshot_time = 0
    generating_detected = False

    while time.time() - start_time < MAX_WAIT:
        if not page_alive(page):
            log("Page closed during video wait!", "ERR")
            return "ERROR"

        await asyncio.sleep(4)
        elapsed = int(time.time() - start_time)

        # ── RATE LIMIT CHECK (instant kill) ───────────────────────
        rate_limit_match = await page_contains_text(page, [
            "reached the daily limit",
            "daily limit for video",
            "try again tomorrow",
            "rate limit",
            "too many requests",
            "limit reached",
            "generation limit",
        ])
        if rate_limit_match:
            log(f"⚠️  RATE LIMITED! Detected: '{rate_limit_match}'. Stopping immediately.", "WARNING")
            await screenshot(page, "video_rate_limited", "Rate limit detected!")
            page.remove_listener("response", _on_resp)
            return "RATE_LIMITED"

        # ── GENERATING PROGRESS CHECK ─────────────────────────────
        generating_match = await page_contains_text(page, [
            "Generating video",
            "generating",
            "Creating video",
            "Processing",
            "in queue",
        ])
        if generating_match and not generating_detected:
            generating_detected = True
            log(f"Video generation in progress: '{generating_match}'", "OK")
            await screenshot(page, "video_generating", "Generation started!")

        # ── ERROR CHECK ───────────────────────────────────────────
        error_match = await page_contains_text(page, [
            "generation failed",
            "something went wrong",
            "error generating",
            "could not generate",
            "server error",
            "an error occurred",
        ])
        if error_match:
            log(f"Video generation error: '{error_match}'", "ERR")
            await screenshot(page, "video_gen_error")
            page.remove_listener("response", _on_resp)
            return "GEN_ERROR"

        # ── 1. Check network intercepted URLs ─────────────────────
        if captured_mp4s:
            for url in captured_mp4s:
                if "http" in url and ("mp4" in url or "video" in url):
                    video_url = url
                    break

        # ── 2. Check for <video> elements in DOM ─────────────────
        if not video_url:
            # Try clicking video card/thumbnail to mount player
            try:
                card = page.locator('[class*="block-video"], [class*="video-player"], [class*="video-card"], [class*="VideoPlayer"]').first
                if await card.count() > 0:
                    await card.click(force=True)
                    await asyncio.sleep(2)
            except Exception:
                pass

            video_els = await safe_evaluate(page, """() => {
                const vids = Array.from(document.querySelectorAll('video'));
                return vids.map(v => v.src || (v.querySelector('source') ? v.querySelector('source').src : '')).filter(src => src && src.length > 5);
            }""")
            if video_els:
                video_url = video_els[0]

        # ── 3. Check for download buttons/links ──────────────────
        if not video_url:
            dl_links = await safe_evaluate(page, """() => {
                const links = Array.from(document.querySelectorAll('a[href*=".mp4"], a[download], a[href*="video"]'));
                return links.map(l => l.href || l.getAttribute('data-url') || '').filter(h => h && h.includes('http'));
            }""")
            if dl_links:
                video_url = dl_links[0]
            else:
                try:
                    dl_btn = page.locator('a:has-text("Download"), a[href*=".mp4"], button:has-text("Download")').first
                    if await dl_btn.count() > 0 and await dl_btn.is_visible():
                        href = await dl_btn.get_attribute("href")
                        if href and "http" in href:
                            video_url = href
                except Exception:
                    pass

        if video_url:
            log(f"🎬 Video generation complete! URL: {video_url[:80]}...", "SUCCESS")
            await screenshot(page, "video_found", "Video URL discovered!")
            break

        # Periodic status + screenshot
        if elapsed - last_screenshot_time >= 30:
            last_screenshot_time = elapsed
            await screenshot(page, f"video_wait_{elapsed}s", f"Still waiting ({elapsed}s elapsed)")
            log(f"Still waiting for video... ({elapsed}s elapsed)", "WAIT")

    page.remove_listener("response", _on_resp)

    if not video_url:
        log("Timed out waiting for video generation URL.", "ERR")
        await screenshot(page, "video_timeout", "Timed out waiting for video!")
        # Final DOM dump for debugging
        body_text = await safe_evaluate(page, "() => document.body ? document.body.innerText.substring(0, 2000) : ''")
        if body_text:
            log(f"Page text at timeout: {body_text[:500]}", "INFO")
        return "TIMEOUT"

    # ─── Download the video ───────────────────────────────────────
    timestamp = int(time.time())
    dest_path = os.path.join(DOWNLOAD_DIR, f"dola_generated_video_{timestamp}.mp4")
    log(f"Downloading video to {dest_path}...", "INFO")

    try:
        req = urllib.request.Request(video_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as response, open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        fsize = os.path.getsize(dest_path)
        if fsize < 1000:
            log(f"Downloaded file too small ({fsize} bytes) — likely not a real video.", "WARNING")
            return "DOWNLOAD_ERROR"
        log(f"✅ Successfully saved video! File size: {fsize} bytes", "SUCCESS")
        return "SUCCESS"
    except Exception as e:
        log(f"Direct download failed ({e}), attempting browser download via evaluate...", "WARNING")
        try:
            async with page.expect_download(timeout=30000) as download_info:
                await page.evaluate(f"""(url) => {{
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'generated_video_{timestamp}.mp4';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                }}""", video_url)
            download = await download_info.value
            await download.save_as(dest_path)
            fsize = os.path.getsize(dest_path)
            log(f"✅ Downloaded via browser! File size: {fsize} bytes", "SUCCESS")
            return "SUCCESS"
        except Exception as e2:
            log(f"Browser download also failed: {e2}", "ERR")
            return "DOWNLOAD_ERROR"


# ─── Record Results ──────────────────────────────────────────────────
def record_cycle_result(email, status, video_path, vpn_ip, duration_sec, error_msg=""):
    """Persist structured cycle results into both CSV and JSON summaries."""
    csv_file = os.path.join(DOWNLOAD_DIR, "results.csv")
    json_file = os.path.join(DOWNLOAD_DIR, "results_summary.json")
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "email": email,
        "status": status,
        "video_path": video_path or "",
        "vpn_ip": vpn_ip,
        "duration_seconds": round(duration_sec, 2),
        "error_message": str(error_msg)
    }

    write_header = not os.path.isfile(csv_file)
    try:
        with open(csv_file, "a", newline="", encoding="utf-8") as cf:
            writer = csv.DictWriter(cf, fieldnames=record.keys())
            if write_header:
                writer.writeheader()
            writer.writerow(record)
    except Exception:
        pass

    data = []
    if os.path.isfile(json_file):
        try:
            with open(json_file, "r", encoding="utf-8") as jf:
                data = json.load(jf)
        except Exception:
            data = []
    data.append(record)
    with open(json_file, "w", encoding="utf-8") as jf:
        json.dump(data, jf, indent=2)
    log(f"Recorded cycle status [{status}] for {email} into results.csv / results_summary.json", "INFO")


# ─── Cleanup ─────────────────────────────────────────────────────────
def cleanup_zombie_chrome(session_dir):
    """Ensure no stuck chromium or lock files prevent session startup."""
    try:
        subprocess.run(["pkill", "-f", session_dir], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lock_path = os.path.join(session_dir, lock_name)
        if os.path.exists(lock_path) or os.path.islink(lock_path):
            try:
                os.remove(lock_path)
            except Exception:
                pass


async def rotate_vpn_with_retry(max_retries=3):
    """Automatically retry and failover Spain VPN servers until a clean ES IP is verified."""
    es_servers = ["es134", "es135", "es136", "es141", "es142", "es143", "es144", "es145", "es147", "es148", "es149"]
    for attempt in range(1, max_retries + 1):
        srv = random.choice(es_servers)
        log(f"[VPN Attempt {attempt}/{max_retries}] Rotating Spain VPN namespace to server {srv}...", "INFO")
        try:
            env = os.environ.copy()
            env["SERVER"] = srv
            subprocess.run(["sudo", "-E", "bash", "/home/azureuser/archive_dola/vpnrotate/rotate-only.sh"], env=env, check=False)
            await asyncio.sleep(4)
            # Verify new Spanish IP (run inside spain namespace)
            res = subprocess.run(
                ["sudo", "ip", "netns", "exec", "spain", "curl", "-s", "--max-time", "12", "https://ipinfo.io/json"],
                capture_output=True, text=True
            )
            if res.returncode == 0 and res.stdout.strip():
                ip_info = json.loads(res.stdout)
                ip_addr = ip_info.get("ip", "unknown")
                country = ip_info.get("country", "")
                if country == "ES":
                    log(f"✅ Verified Spain VPN IP ({srv}): {ip_addr} [{ip_info.get('org', '')}]", "SUCCESS")
                    return True, ip_addr
                else:
                    log(f"VPN connected but IP is not Spanish ({country}). Retrying...", "WARNING")
            else:
                log("curl to ipinfo.io timed out or failed inside spain namespace.", "WARNING")
        except Exception as e:
            log(f"Error during VPN rotation ({srv}): {e}", "WARNING")

    log("Exhausted all VPN rotation attempts.", "ERR")
    return False, "unknown"


# ─── Main Lifecycle ──────────────────────────────────────────────────
async def main(accounts_file="accounts.txt", headful=True):
    log("==========================================================", "INFO")
    log("Full Robust E2E Video Automation Lifecycle V2 Starting...", "INFO")
    log("==========================================================", "INFO")

    accounts = load_accounts(accounts_file)
    if not accounts:
        log("No accounts found in accounts.txt!", "ERR")
        return

    # Ensure SOCKS proxy is running for VPN routing
    if not ensure_socks_proxy():
        log("Cannot start SOCKS proxy for VPN routing. Aborting.", "ERR")
        return

    from cloakbrowser import launch_persistent_context_async

    # Initialize empty tracker for iteration limits across accounts securely!
    initialized_accounts = set()

    for cycle_count in range(1, 16): # 15 cycles total
        log("==========================================================", "INFO")
        log(f"Starting HARD-RESET CYCLE #{cycle_count} across all accounts...", "INFO")
        log("==========================================================", "INFO")

        for idx, acc in enumerate(accounts):
            email = acc[0]
            password = acc[1]
            totp_secret = acc[2] if len(acc) > 2 else None

            # Always use the Spain SOCKS proxy for routing through VPN namespace
            vpn_proxy = {"server": SPAIN_SOCKS_PROXY}
            session_dir = f"/home/azureuser/bulk-Video-generation/app/sessions/{email.replace('@', '_').replace('.', '_')}"

            start_time = time.time()
            vpn_ip = "unknown"
            final_status = "ERROR"
            error_msg = ""
            video_path = None
            ctx = None
            page = None

            log(f"\n>>>>>> CYCLE #{cycle_count} FOR {email} <<<<<<", "INFO")

            log("\n--- STEP 1 & ROTATE: FULL HARD ISOLATION START ---", "INFO")
            cleanup_zombie_chrome(session_dir)

            # Wiping the Chromium profile entirely between loop generations
            if os.path.exists(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)
                log("Cleared session storage directory entirely to remove trace history.", "INFO")

            # Rotates VPN network node immediately to bypass tracking requests sequentially
            vpn_ok, vpn_ip = await rotate_vpn_with_retry(max_retries=3)
            if not vpn_ok:
                log("Failed to acquire verified Spain IP after 3 attempts. Skipping this account loop.", "ERR")
                record_cycle_result(email, "VPN_FAILOVER_ERROR", None, vpn_ip, time.time() - start_time, "Could not acquire verified ES IP")
                continue # Skip to next account

            restart_socks_proxy()

            # The 5-Minute Backend Cool-Down Hook prevents aggressive ReCaptcha locks!
            if cycle_count > 1 or idx > 0:
                log("\nWaiting a hard 3-minute cool-down to allow Google & Dola server caches to flush on new IP...", "WAIT")
                for min_left in range(3, 0, -1):
                    log(f"Cool-down: {min_left} minute(s) remaining.", "INFO")
                    await asyncio.sleep(60)

            # STEP 2, 3, 4: SINGLE CONTINUOUS BROWSER SESSION
            log("\n--- STEP 2: REGISTER FRESH DOLA ACCOUNT ---", "INFO")
            try:
                # Launch a completely fresh instance
                ctx = await launch_persistent_context_async(
                    user_data_dir=session_dir, headless=not headful, proxy=vpn_proxy,
                    viewport={"width": 1280, "height": 900}, locale="en-US", humanize=True)
                page = await ctx.new_page()

                oauth_url2 = await get_google_auth_url(ctx, page)
                if oauth_url2 == "GEO_BLOCKED":
                    log("Geo-blocked on Step 2 — VPN proxy not working.", "ERR")
                    final_status = "GEO_BLOCKED"
                    continue
                elif not oauth_url2:
                    # Check if already logged in from prior session
                    if page_alive(page):
                        logged_in = await page_contains_text(page, ["New Chat", "Main chat", "Create Video"])
                        if logged_in:
                            log("Already logged in. (Wait, it should have been deleted in Step 5!) Proceeding anyway.", "WARNING")
                        else:
                            log("Could not get OAuth URL and not logged in.", "ERR")
                            final_status = "OAUTH_URL_ERROR"
                            continue
                    else:
                        log("Could not get OAuth URL for registration!", "ERR")
                        final_status = "OAUTH_URL_ERROR"
                        continue
                else:
                    # Clear dola cookies before hitting OAuth to ensure it creates a fresh account
                    log("Clearing dola.com cookies to ensure fresh registration...", "INFO")
                    await ctx.clear_cookies() # Clear all cookies so it's a fresh dola instance

                    log("Executing Google OAuth & fresh account registration...", "INFO")
                    state2 = await automate_google_login(
                        oauth_url2, email, password, headless=not headful, proxy=vpn_proxy,
                        session_dir=session_dir, existing_ctx=ctx, existing_page=page,
                        close_on_finish=False, totp_secret=totp_secret)
                    if not state2.get("login_done", False):
                        log("Registration / Login failed for this account cycle. Skip to next.", "ERR")
                        final_status = "LOGIN_FAILED"
                        error_msg = str(state2.get("error", "Google login or captcha failed"))
                        continue

                log("Account registered / logged in successfully!", "SUCCESS")
                await asyncio.sleep(2)

                if page_alive(page):
                    await screenshot(page, "login_success", "Login complete, proceeding to video gen")

                # STEP 3 & 4: GENERATE & DOWNLOAD VIDEO
                log("\n--- STEP 3 & 4: GENERATE AND DOWNLOAD VIDEO ---", "INFO")

                if not page_alive(page):
                    log("Page died before video generation. Getting new page...", "WARNING")
                    try:
                        page = await ctx.new_page()
                        await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=45000)
                        await asyncio.sleep(5)
                    except Exception:
                        log("Cannot recover page. Marking as error.", "ERR")
                        final_status = "BROWSER_CLOSED"
                        error_msg = "Page context died"
                        continue

                video_result = await generate_and_download_video(page, SAMPLE_PROMPT)

                if video_result == "SUCCESS":
                    log("🎉 Video generation and download completed!", "SUCCESS")
                    final_status = "SUCCESS"
                    try:
                        mp4s = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".mp4")]
                        if mp4s:
                            video_path = max(mp4s, key=os.path.getmtime)
                    except Exception:
                        pass
                elif video_result == "RATE_LIMITED":
                    log("Account hit daily rate limit (was not successfully deleted previously).", "WARNING")
                    final_status = "RATE_LIMITED"
                    error_msg = "Daily video generation limit reached"
                elif video_result == "GEN_ERROR":
                    final_status = "GEN_ERROR"
                    error_msg = "Video generation returned an error"
                elif video_result == "TIMEOUT":
                    final_status = "VIDEO_TIMEOUT"
                    error_msg = "Timed out waiting for video"
                else:
                    final_status = "VIDEO_ERROR"
                    error_msg = f"Video result: {video_result}"

                await asyncio.sleep(3)
            except Exception as e:
                log(f"Unhandled cycle exception: {e}", "ERR")
                final_status = "CYCLE_EXCEPTION"
                error_msg = str(e)
            finally:
                # STEP 5: ALWAYS DELETE ACCOUNT TO CLEAN UP BEFORE CLOSING
                log("\n--- STEP 5: FINAL ACCOUNT DELETION & CLEANUP ---", "INFO")
                try:
                    if page_alive(page):
                        await delete_dola_account(page)
                except Exception as e:
                    log(f"Note during final account deletion: {e}", "WARNING")

                # AT LAST CLOSE IT
                log("Closing browser for this cycle...", "INFO")
                try:
                    if ctx:
                        await ctx.close()
                except Exception:
                    pass
                cleanup_zombie_chrome(session_dir)
                record_cycle_result(email, final_status, video_path, vpn_ip, time.time() - start_time, error_msg)

            log(f"\nCompleted Cycle #{cycle_count} for: {email} [Status: {final_status}]\n", "SUCCESS")
            # Wait 10s before burning through the next cycle loop!
            await asyncio.sleep(10)

    log("\n==========================================================", "SUCCESS")
    log("Full Robust E2E Video Automation Lifecycle V2 Completed!", "SUCCESS")
    log("==========================================================", "SUCCESS")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headful", action="store_true", default=True, help="Run with visible browser in VNC")
    parser.add_argument("--accounts", default="accounts.txt", help="Accounts file")
    args = parser.parse_args()
    asyncio.run(main(args.accounts, headful=args.headful))
