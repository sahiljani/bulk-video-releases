#!/usr/bin/env python3
"""CloakBrowser batch auto-login driver for the Flask OAuth app.

Talks to app/main.py (must be running on :5000): asks for an OAuth URL,
drives Google's login screens in CloakBrowser, then polls for the result.
No proxy — direct connections only.
"""

import requests

import store
from store import has_account
from proxies import load_proxies, proxy_for_index

APP_BASE = "http://localhost:5000"

CONSENT_JS = r"""
() => {
  const knownIds = ['submit_approve_access', 'approve_button', 'confirm'];
  for (const id of knownIds) {
    const el = document.getElementById(id);
    if (el && el.offsetParent !== null) { el.click(); return 'id:' + id; }
  }
  const knownNames = ['confirm', 'continue', 'approve', 'accept'];
  for (const name of knownNames) {
    const el = document.querySelector(`[name="${name}"]`);
    if (el && el.offsetParent !== null) { el.click(); return 'name:' + name; }
  }
  const buttons = document.querySelectorAll(
    'button, [role="button"], span[role="button"], input[type="submit"], ' +
    'span.VfPpkd-vQzf8d, div.VfPpkd-RLmnJb, [jsname="V67aGc"]');
  const texts = ['i understand','i agree','agree','allow','continue','approve',
    'confirm','accept','got it','accept all','done','i accept','accept & continue',
    'sign in','log in','get started','proceed'];
  for (const btn of buttons) {
    const txt = (btn.textContent || btn.value || '').toLowerCase().trim();
    if (texts.some(t => txt.includes(t))) {
      btn.click();
      if (btn.tagName === 'SPAN' && btn.parentElement && btn.parentElement.tagName === 'BUTTON')
        btn.parentElement.click();
      return 'text:' + txt;
    }
  }
  const adv = document.querySelector('#advancedButton') || document.querySelector('[id*="advanced"]');
  if (adv) { adv.click(); return 'advanced'; }
  return null;
}
"""


def parse_account(line):
    """'email:password[:totp_secret][:proxy]' -> (email, password, totp_secret, proxy); None for blank/comment."""
    line = line.strip()
    if not line or line.startswith("#") or ":" not in line:
        return None
    parts = [p.strip() for p in line.split(":")]
    email = parts[0]
    password = parts[1] if len(parts) > 1 else ""
    totp_secret = None
    proxy = None
    for p in parts[2:]:
        clean_p = p.replace(" ", "").upper()
        if len(clean_p) >= 16 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in clean_p):
            totp_secret = clean_p
        elif "http" in p.lower() or "socks" in p.lower() or "." in p:
            proxy = p
    return email, password, totp_secret, proxy


def load_accounts(path):
    accounts = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            acc = parse_account(line)
            if acc:
                accounts.append(acc)
    return accounts


def left_google(url, content=""):
    """True once the browser reaches our callback / leaves google.com / shows success."""
    if content and "Login Success" in content:
        return True
    if not url:
        return False
    if "dola.com" in url or "localhost:5000/callback" in url or "127.0.0.1:5000/callback" in url:
        return True
    return "google." not in url


def server_healthy(base=APP_BASE):
    try:
        r = requests.get(f"{base}/health", timeout=5)
        return r.ok and r.json().get("ok") is True
    except requests.RequestException:
        return False


def get_oauth_url(base=APP_BASE, test=False):
    """POST /api/login-url -> (oauth_url, state) or (None, None).

    test=True tells the server to skip persisting the resulting tokens.
    """
    try:
        r = requests.post(f"{base}/api/login-url", json={"test": True} if test else {}, timeout=30)
        data = r.json()
    except (requests.RequestException, ValueError):
        return None, None
    if "oauth_url" in data:
        return data["oauth_url"], data["state"]
    return None, None


def check_login_status(state, base=APP_BASE):
    """GET /api/login-status?state=... -> status dict."""
    try:
        r = requests.get(f"{base}/api/login-status", params={"state": state}, timeout=5)
        return r.json()
    except (requests.RequestException, ValueError):
        return {"status": "unknown"}


import asyncio
import os
import time
from urllib.parse import urlparse

DEBUG = os.getenv("DEBUG", "false").lower() == "true"


def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    pfx = {"INFO": "i", "OK": "OK", "ERR": "XX", "DBG": "..", "WAIT": ".."}.get(level, " ")
    print(f"[{ts}] {pfx} {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        log(msg, "DBG")


async def solve_recaptcha(page):
    try:
        frames = page.frames
        challenge_frame = None
        for frame in frames:
            if 'recaptcha/api2/bframe' in frame.url or 'recaptcha/enterprise/bframe' in frame.url:
                challenge_frame = frame
                break
        
        if not challenge_frame:
            return False
            
        log("Handling ReCaptcha Audio Challenge...", "INFO")
        
        # Click audio button
        audio_btn = challenge_frame.locator("#recaptcha-audio-button")
        try:
            await audio_btn.wait_for(state="attached", timeout=5000)
            if await audio_btn.count() > 0:
                await audio_btn.click(force=True)
        except Exception:
            pass
        await asyncio.sleep(3)
        
        # Check for block
        content = await challenge_frame.content()
        if "Try again later" in content or "Computer Network" in content:
            log("ReCaptcha blocked the IP (Try again later).", "ERROR")
            return False
            
        # Get audio src
        audio_src_loc = challenge_frame.locator("#audio-source")
        try:
            await audio_src_loc.wait_for(state="attached", timeout=5000)
        except Exception:
            pass
            
        if await audio_src_loc.count() == 0:
            log("ReCaptcha audio-source not found in challenge frame.", "WARNING")
            await page.screenshot(path="/home/azureuser/debug_missing_audio.png")
            with open("/home/azureuser/debug_challenge_frame.html", "w", encoding="utf-8") as f:
                f.write(await challenge_frame.content())
            return False
            
        audio_url = await audio_src_loc.get_attribute("src")
        if not audio_url:
            log("ReCaptcha audio-source had no src.", "WARNING")
            await page.screenshot(path="/home/azureuser/debug_missing_audio.png")
            return False
            
        log("Downloading audio challenge...", "INFO")
        import requests
        import os
        import random
        
        mp3_path = f"/home/azureuser/captcha_{random.randint(1000,9999)}.mp3"
        r = requests.get(audio_url)
        with open(mp3_path, 'wb') as f:
            f.write(r.content)
            
        log("Transcribing audio with SpeechRecognition (Google Web Speech API)...", "INFO")
        try:
            import speech_recognition as sr
            from pydub import AudioSegment
            
            wav_path = mp3_path.replace(".mp3", ".wav")
            # Convert mp3 to wav
            sound = AudioSegment.from_mp3(mp3_path)
            sound.export(wav_path, format="wav")
            
            r_sr = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = r_sr.record(source)
                text = r_sr.recognize_google(audio_data)
                
            log(f"Transcription: {text}", "INFO")
            
            # Clean up files
            if os.path.exists(mp3_path): os.remove(mp3_path)
            if os.path.exists(wav_path): os.remove(wav_path)
            
            input_box = challenge_frame.locator("#audio-response")
            await input_box.fill(text.lower())
            await asyncio.sleep(1)
            
            verify_btn = challenge_frame.locator("#recaptcha-verify-button")
            await verify_btn.click()
            await asyncio.sleep(3)
            
            return True
            
        except Exception as e:
            if os.path.exists(mp3_path): os.remove(mp3_path)
            try:
                if os.path.exists(wav_path): os.remove(wav_path)
            except Exception: pass
            
            log(f"SpeechRecognition error: {e}", "WARNING")
            log("Giving you 30 seconds to solve it manually in VNC...", "WAIT")
            await asyncio.sleep(30)
            return False
            
    except Exception as e:
        log(f"ReCaptcha solver error: {e}", "ERROR")
        return False

async def automate_google_login(oauth_url, email, password, headless=False, proxy=None,
                                session_dir=None, existing_ctx=None, existing_page=None, close_on_finish=True, totp_secret=None):
    """Open the OAuth URL in CloakBrowser, drive Google login, wait for callback."""
    log(f"Opening OAuth URL for {email}..."
        + (f" (proxy {proxy['server']})" if proxy else "")
        + (f" (session {os.path.basename(session_dir)})" if session_dir else ""))

    browser = None
    if existing_ctx and existing_page:
        ctx = existing_ctx
        page = existing_page
    elif session_dir:
        from cloakbrowser import launch_persistent_context_async
        os.makedirs(session_dir, exist_ok=True)
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 900}, locale="en-US", humanize=True)
        page = await ctx.new_page()
    else:
        from cloakbrowser import launch_async
        browser = await launch_async(headless=headless, humanize=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="en-US", proxy=proxy)
        page = await ctx.new_page()

    page.set_default_timeout(30000)
    
    # Intercept requests to save them temporarily
    requests_log = []
    page.on("request", lambda r: requests_log.append(f"[{time.strftime('%H:%M:%S')}] {r.method} {r.url}"))

    async def _auto_dismiss(dialog):
        try:
            await dialog.dismiss()
        except:
            pass
    page.on("dialog", lambda d: asyncio.ensure_future(_auto_dismiss(d)))

    captcha_solved = False
    state = {"login_done": False, "error": None}
    try:
        await page.goto(oauth_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        url = page.url
        log(f"Page: {url[:80]}...")

        if "accounts.google.com" in url or "accounts.google.co" in url:
            log("On Google login page. Automating...", "OK")
            await _handle_google_login(page, email, password, totp_secret=totp_secret)

        for i in range(120):
            if state["login_done"]:
                break
            await asyncio.sleep(0.5)
            try:
                url = page.url
                content = await page.content()
            except Exception:
                continue

            if left_google(url, content):
                state["login_done"] = True
                log(f"Left Google / hit callback: {url[:80]}", "OK")
                break

            if ("Verify it's you" in content or "Verify it\u2019s you" in content or "Confirm you" in content or "reCAPTCHA" in content) and not captcha_solved:
                log("Detected ReCaptcha. Attempting to solve...", "WAIT")
                
                # We first need to click the checkbox if it's there
                try:
                    frames = page.frames
                    checkbox_frame = None
                    for frame in frames:
                        title = await frame.title()
                        if 'api2/anchor' in frame.url or 'recaptcha' in title.lower():
                            checkbox_frame = frame
                            break
                    if checkbox_frame:
                        anchor = checkbox_frame.locator("#recaptcha-anchor")
                        if await anchor.count() > 0:
                            await anchor.click(force=True)
                            await asyncio.sleep(2)
                except Exception as e:
                    dbg(f"Error clicking ReCaptcha anchor: {e}")
                
                solved = await solve_recaptcha(page)
                if solved:
                    log("ReCaptcha solved successfully!", "SUCCESS")
                    captcha_solved = True
                    # Wait for Google to process the captcha solve
                    await asyncio.sleep(5)
                    # Try clicking Next/Continue/Verify button after captcha
                    try:
                        next_btn = page.locator('button:has-text("Next"), button:has-text("Continue"), button:has-text("Verify"), input[type="submit"]').first
                        if await next_btn.count() > 0:
                            await next_btn.click(force=True)
                            log("Clicked Next/Continue after captcha solve", "INFO")
                            await asyncio.sleep(3)
                    except Exception:
                        pass
                else:
                    log("Failed to solve ReCaptcha.", "WARNING")
                
                await asyncio.sleep(2)
                continue
                
            if "When's your birthday?" in content or "birthday" in content.lower():
                log("Handling birthday prompt...", "WAIT")
                await page.screenshot(path="/home/azureuser/debug_birthday_prompt.png")
                with open("/home/azureuser/debug_birthday_dom.html", "w", encoding="utf-8") as f:
                    f.write(content)
                with open("/home/azureuser/debug_requests.txt", "w", encoding="utf-8") as f:
                    f.write("\n".join(requests_log))
                try:
                    # Look for standard Date input or custom Dola specific selectors
                    loc = page.locator('input[placeholder="YYYY-MM-DD"], input[type="date"]').first
                    if await loc.count() > 0 and await loc.is_visible():
                        # Click to focus the picker
                        await loc.click(force=True)
                        await asyncio.sleep(0.5)
                        
                        # Try to fill it normally
                        try:
                            await loc.fill("2000-01-01")
                        except:
                            # Fallback: type it out character by character
                            await loc.press_sequentially("2000-01-01")
                            
                        await asyncio.sleep(0.5)
                        
                        # Press Enter to select the date from the popup if it's a custom picker
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.5)
                        
                        # Force the value via JS just in case React didn't catch it
                        await page.evaluate('''() => {
                            const input = document.querySelector('input[placeholder="YYYY-MM-DD"], input[type="date"]');
                            if(input) {
                                input.value = "2000-01-01";
                                input.dispatchEvent(new Event('input', { bubbles: true }));
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }''')
                        
                        await asyncio.sleep(0.5)
                        await page.screenshot(path="/home/azureuser/debug_birthday_filled.png")
                        
                        next_btn = page.locator("button:has-text('Next'), button:has-text('Continue')").first
                        if await next_btn.count() > 0:
                            await next_btn.click(force=True)
                            
                        await asyncio.sleep(2)
                        await page.screenshot(path="/home/azureuser/debug_birthday_after_next.png")
                        continue
                except Exception as e:
                    dbg(f"Birthday prompt error: {e}")

            if "Log In to Unlock More Features" in content:
                log("Handling secondary login modal...", "WAIT")
                try:
                    google_btn = page.locator(
                        'button:has-text("Continue with Google"), '
                        '[class*="button-"]:has-text("Continue with Google"), '
                        'button:has(img.size-24), '
                        '[class*="button-"]:has(img.size-24)'
                    ).first
                    if await google_btn.count() > 0 and await google_btn.is_visible():
                        await google_btn.click(force=True)
                        await asyncio.sleep(2)
                        continue
                except Exception as e:
                    dbg(f"Secondary modal error: {e}")

            if left_google(url, content) and "from_logout=1" not in url and "When's your birthday?" not in content:
                log(f"Left Google / hit callback: {url[:80]}", "OK")
                for _ in range(15):
                    if "callback" not in page.url and "/chat" in page.url:
                        break
                    await asyncio.sleep(1)
                await asyncio.sleep(3)
                state["login_done"] = True
                break
            if DEBUG and i % 10 == 0 and i > 0:
                dbg(f"Still waiting... ({i * 0.5:.0f}s) url={url[:60]}")
    except Exception as e:
        log(f"Browser error: {e}", "ERR")
        state["error"] = str(e)
    finally:
        if close_on_finish:
            try:
                # Persistent context: close the context (flushes the profile to disk).
                # Ephemeral: close the whole browser.
                await (browser.close() if browser is not None else ctx.close())
            except Exception:
                pass
    return state


async def _handle_google_login(page, email, password, totp_secret=None):
    """Fill Google's login screens: email -> password -> 2FA/TOTP -> consent -> redirect."""
    log(f"[{email}] On Google login page. Automating..." + (f" (with 2FA TOTP)" if totp_secret else ""))
    for attempt in range(120):
        # Guard: abort if page or context is gone
        try:
            if page.is_closed():
                log(f"[{email}] Page is closed. Login flow ending.", "OK")
                return
        except Exception:
            log(f"[{email}] Page unavailable. Login flow ending.", "OK")
            return
        try:
            url = page.url
        except Exception:
            log(f"[{email}] Page closed/navigated away", "OK")
            return

        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            hostname = ""

        if "google" not in hostname:
            log(f"[{email}] Left Google. Now at: {url[:80]}", "OK")
            # If we landed on Dola's callback, wait for SPA to process the hash token and redirect
            if "dola.com/auth/callback" in url:
                log(f"[{email}] Waiting for Dola SPA to process callback and redirect...", "INFO")
                for _ in range(30):
                    try:
                        curr_url = page.url
                        if "dola.com/chat" in curr_url and "auth/callback" not in curr_url:
                            log(f"[{email}] SPA successfully redirected to chat!", "OK")
                            break
                        await asyncio.sleep(1)
                    except Exception:
                        break
            return

        try:
            content = await page.content()
            if "Login Success" in content:
                log(f"[{email}] Login Success detected in page!", "OK")
                return
            if hostname in ("localhost", "127.0.0.1", "www.dola.com", "dola.com"):
                log(f"[{email}] On callback page — login complete!", "OK")
                return
        except Exception:
            pass

        # ── Handle "Something went wrong" Popup ──
        try:
            if not page.is_closed():
                restart_btn = page.locator('span:has-text("Restart")').first
                if await restart_btn.is_visible():
                    log(f"[{email}] 'Something went wrong' popup detected. Clicking Restart...", "WARNING")
                    await restart_btn.click(force=True)
                    await asyncio.sleep(2)
                    continue
        except Exception:
            pass

        # ── Detect ReCaptcha ──
        if "Verify it's you" in content or "Verify it’s you" in content or "Confirm you" in content or "reCAPTCHA" in content:
            # First ensure there's actually a challenge frame before entering the loop block
            challenge_present = False
            for f in page.frames:
                if 'recaptcha/api2/' in f.url or 'recaptcha/enterprise/' in f.url:
                    challenge_present = True
                    break

            if challenge_present:
                log(f"[{email}] Detected ReCaptcha during login. Calling solver...", "WAIT")
                try:
                    frames = page.frames
                    checkbox_frame = None
                    for frame in frames:
                        title = await frame.title()
                        if 'api2/anchor' in frame.url or 'recaptcha' in title.lower():
                            checkbox_frame = frame
                            break
                    if checkbox_frame:
                        anchor = checkbox_frame.locator("#recaptcha-anchor")
                        if await anchor.count() > 0:
                            log("Clicking ReCaptcha anchor checkbox...", "INFO")
                            await anchor.click(force=True)
                            await asyncio.sleep(3)
                except Exception as e:
                    log(f"Error clicking checkbox: {e}", "ERR")
                    pass

                solved = await solve_recaptcha(page)
                if solved:
                    log("ReCaptcha solved successfully!", "SUCCESS")
                    await asyncio.sleep(2)
                else:
                    log("Failed to solve ReCaptcha.", "WARNING")
                    # Try dismissing it or clicking next randomly to break loop
                    try:
                        next_btn = page.locator('button:has-text("Next"), button:has-text("Skip")').last
                        if await next_btn.count() > 0 and await next_btn.is_visible():
                            await next_btn.click(force=True)
                    except Exception:
                        pass
                    await asyncio.sleep(4)
                continue
            else:
                pass # Text exists but no actual iframe, continue down to password/TOTP logic

        # ── Email step ──
        try:
            email_visible = await page.evaluate(
                "() => { const el = document.querySelector('#identifierId');"
                " return el && el.offsetParent !== null; }"
            )
        except Exception:
            email_visible = False

        if email_visible:
            dbg(f"[{email}] Filling Google email...")
            loc = page.locator("#identifierId").first
            await loc.click(force=True)
            await asyncio.sleep(0.2)
            await loc.press("Control+a")
            await loc.press("Backspace")
            await loc.press_sequentially(email, delay=40)
            await asyncio.sleep(0.3)
            await page.evaluate(
                "() => { const b = document.querySelector('#identifierNext button');"
                " if (b) b.click(); }"
            )
            for _ in range(10):
                await asyncio.sleep(0.5)
                try:
                    if await page.evaluate(
                        "() => { for (const el of document.querySelectorAll("
                        "'input[name=\"Passwd\"], input[type=\"password\"]'))"
                        " { if (el.offsetParent !== null) return true; } return false; }"
                    ):
                        break
                except Exception:
                    pass
            await asyncio.sleep(0.5)
            await asyncio.sleep(0.5)
            continue
            
        # ── Password step ──
        try:
            if page.is_closed():
                return
            pwd_input = page.locator('input[type="password"]')
            if await pwd_input.is_visible():
                log(f"[{email}] Password field visible. Entering password...", "OK")
                await pwd_input.fill(password)
                if not page.is_closed():
                    await page.keyboard.press("Enter")
                await asyncio.sleep(2)
                continue
        except Exception as e:
            dbg(f"[{email}] Password field error (likely navigating): {e}")

        # ── Selection Screen (Choose how you want to sign in) ──
        try:
            # Only trigger this if there is NO password input field visible!
            # Since we moved the password check above, this is safe, but we can also add a negative assertion just in case:
            if not await page.locator('input[type="password"]').is_visible():
                enter_pwd_txt = page.locator("text='Enter your password'").locator("visible=true").first
                if await enter_pwd_txt.count() > 0:
                    log(f"[{email}] 'Enter your password' option detected on selection screen. Clicking...", "WAIT")
                    try:
                        await enter_pwd_txt.click(force=True)
                        await asyncio.sleep(1.5)
                        continue
                    except Exception as e:
                        log(f"[{email}] Click failed: {e}", "WARNING")
        except Exception:
            pass

        # ── Dismiss popups (Save Password, Confirm Recovery, etc.) ──
        # ── Password step ──
        try:
            pwd_visible = await page.evaluate(
                "() => { for (const el of document.querySelectorAll("
                "'input[name=\"Passwd\"], input[type=\"password\"]'))"
                " { if (el.offsetParent !== null) return true; } return false; }"
            )
        except Exception:
            pwd_visible = False

        if pwd_visible:
            dbg(f"[{email}] Filling Google password...")
            try:
                loc = page.locator('input[name="Passwd"]').first
                if await loc.count() == 0 or not await loc.is_visible():
                    loc = page.locator('input[type="password"]').first
                await loc.click(force=True)
                await asyncio.sleep(0.2)
                await loc.press("Control+a")
                await loc.press("Backspace")
                await loc.press_sequentially(password, delay=30)
                await asyncio.sleep(0.2)
                await page.evaluate(
                    "() => { const b = document.querySelector('#passwordNext button');"
                    " if (b) b.click(); }"
                )
            except Exception as e:
                dbg(f"[{email}] Password field error (likely navigating): {e}")
                await asyncio.sleep(1)
                continue
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(1)
            try:
                url = page.url
                if "accounts.google.com" not in url and "accounts.google.co" not in url:
                    log(f"[{email}] Left Google after password. Now at: {url[:60]}", "OK")
                    return
            except Exception:
                return
            continue

        # ── 2FA / TOTP Authenticator step ──
        if totp_secret:
            try:
                # 1. If we are ALREADY on the TOTP entry screen, just enter it!
                totp_input = page.locator('input[name="totpPin"], input[id="totpPin"], input[autocomplete="one-time-code"]').first
                if await totp_input.count() > 0 and await totp_input.is_visible():
                    log(f"[{email}] 2FA Authenticator screen detected! Generating TOTP code...", "WAIT")
                    import pyotp
                    totp = pyotp.TOTP(totp_secret.replace(" ", "").upper())
                    code = totp.now()
                    log(f"[{email}] Entering 2FA TOTP code: {code}", "INFO")
                    await totp_input.click(force=True)
                    await asyncio.sleep(0.2)
                    await totp_input.press("Control+a")
                    await totp_input.press("Backspace")
                    await totp_input.press_sequentially(code, delay=40)
                    await asyncio.sleep(0.3)
                    await totp_input.press("Enter")
                    await asyncio.sleep(0.5)
                    try:
                        next_btn = page.locator('#totpNext button, #idvPreregisteredPhoneNext button, button:has-text("Next"), button:has-text("Verify")').first
                        if await next_btn.count() > 0 and await next_btn.is_visible():
                            await next_btn.click(force=True)
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                    continue

                # 2. If the 'Authenticator app' option is ALREADY visible (list is open), click it
                if await page.get_by_text("Authenticator app", exact=False).count() > 0:
                    log(f"[{email}] Selecting Authenticator app option...", "WAIT")
                    try:
                        await page.get_by_text("Authenticator app", exact=False).last.click(force=True, timeout=1000)
                    except Exception:
                        await page.evaluate("""() => {
                            const els = Array.from(document.querySelectorAll('div, li'));
                            let target = els.find(e => e.innerText && e.innerText.includes("Authenticator app") && (e.getAttribute("data-challengetype") || e.role === "button" || e.tagName === "LI" || e.classList.contains("JDAKTe")));
                            if(target) target.click();
                            else {
                                let generic = els.reverse().find(e => e.innerText && e.innerText.includes("Authenticator app"));
                                if(generic) generic.click();
                            }
                        }""")
                    await asyncio.sleep(2)
                    continue
                
                # 3. If not, look for the 'Try another way' button to open the list
                try_another = page.get_by_text("Try another way", exact=False).last
                if await try_another.count() > 0 and await try_another.is_visible():
                    log(f"[{email}] 'Try another way' detected. Clicking robustly...", "WAIT")
                    await try_another.click(force=True)
                    await asyncio.sleep(1.5)
                    continue
            except Exception as e:
                log(f"[{email}] 2FA detection exception: {e}", "WARNING")
                pass
        pass

        # ── Consent / agreement / speedbump ──
        try:
            consent_clicked = await page.evaluate(CONSENT_JS)
        except Exception as e:
            dbg(f"[{email}] Consent evaluate exception: {e}")
            consent_clicked = None

        if consent_clicked:
            dbg(f"[{email}] Consent: {consent_clicked}")
            if "advanced" in str(consent_clicked):
                await asyncio.sleep(1.5)
                try:
                    await page.evaluate(
                        "() => { for (const el of document.querySelectorAll("
                        "'a, button, [role=\"button\"]')) { const t = (el.textContent||'')"
                        ".toLowerCase(); if (t.includes('go to') || t.includes('unsafe')"
                        " || t.includes('proceed')) { el.click(); return; } } }"
                    )
                    await asyncio.sleep(2)
                except Exception:
                    pass
            await asyncio.sleep(0.3)
            continue

        # ── Account chooser ──
        try:
            picked = await page.evaluate(
                "() => { const a = document.querySelectorAll('[data-identifier], [data-email]');"
                " if (a.length) { a[0].click(); return true; } return false; }"
            )
            if picked:
                dbg(f"[{email}] Picked first account")
                await asyncio.sleep(1)
                continue
        except Exception:
            pass

        await asyncio.sleep(0.5)

    log(f"[{email}] Google login timed out", "ERR")


import argparse
import sys


def filter_new(accounts, force=False):
    # Pass store.TOKENS_FILE explicitly (rather than relying on has_account's
    # bound default) so tests that monkeypatch store.TOKENS_FILE take effect.
    if force:
        return list(accounts)
    return [(e, p) for (e, p) in accounts if not has_account(e, path=store.TOKENS_FILE)]


GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def dot_variants(email, limit=None):
    """Generate Gmail 'dot method' variants of an address.

    Gmail ignores dots in the local part, so j.o.hn@gmail.com and john@gmail.com
    are the SAME mailbox - and log into the SAME Google account. Returns full
    addresses (variant@domain), starting with the dotless canonical form, then
    each combination of dots inserted between local-part characters. Non-Gmail
    addresses are returned unchanged (dots there are significant). Any existing
    dots in the input are normalized away first.
    """
    if "@" not in email:
        return [email]
    local, domain = email.rsplit("@", 1)
    if domain.lower() not in GMAIL_DOMAINS:
        return [email]
    base = local.replace(".", "")
    if len(base) < 2:
        return [f"{base}@{domain}"]
    gaps = len(base) - 1
    total = 1 << gaps  # 2**gaps possible dot placements (incl. none)
    n = total if limit is None else max(1, min(limit, total))
    variants = []
    for mask in range(n):
        chars = [base[0]]
        for i in range(gaps):
            if mask & (1 << i):
                chars.append(".")
            chars.append(base[i + 1])
        variants.append(f"{''.join(chars)}@{domain}")
    return variants


def expand_dot_accounts(accounts, count):
    """Expand each (email, password) into up to `count` Gmail dot-variants."""
    out = []
    for email, pwd in accounts:
        for variant in dot_variants(email, limit=count):
            out.append((variant, pwd))
    return out


SESSIONS_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")


def session_dir_for(email, base=SESSIONS_BASE):
    """Persistent profile directory for an account: base/<sanitized-email>.

    The folder holds the CloakBrowser profile (cookies etc.), so a Google login
    done once is reused on later runs. Sanitizes the email to a filesystem-safe
    name so different accounts never collide.
    """
    safe = "".join(c if c.isalnum() else "_" for c in email.lower())
    return os.path.join(base, safe)


async def process_account(email, password, headless=False, test_only=False, proxy=None,
                          session_base=None):
    log(f"{'[TEST] ' if test_only else ''}Processing: {email}")

    session_dir = session_dir_for(email, session_base) if session_base else None

    oauth_url, state = get_oauth_url(test=test_only)
    if not oauth_url:
        return {"success": False, "email": email, "error": "oauth_url_failed"}

    result = await automate_google_login(oauth_url, email, password,
                                         headless=headless, proxy=proxy,
                                         session_dir=session_dir)
    if result.get("error") and not result.get("login_done"):
        return {"success": False, "email": email, "error": result["error"]}

    log("Checking if token was captured...", "WAIT")
    for _ in range(30):
        status = check_login_status(state)
        if status.get("status") == "ok":
            log(f"Login success! {status.get('email')}", "OK")
            return {"success": True, "email": status.get("email", email)}
        if status.get("status") == "error":
            return {"success": False, "email": email, "error": status.get("error")}
        await asyncio.sleep(1)

    return {"success": False, "email": email, "error": "token_not_captured"}


async def run_batch(accounts, headless=False, concurrent=1, test_only=False, proxies=None,
                    session_base=None):
    proxies = proxies or []
    sem = asyncio.Semaphore(concurrent)

    async def _run(index, email, password):
        async with sem:
            proxy = proxy_for_index(index, proxies)
            return await process_account(email, password, headless=headless,
                                         test_only=test_only, proxy=proxy,
                                         session_base=session_base)

    tasks = [_run(i, e, p) for i, (e, p) in enumerate(accounts)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({"success": False, "email": accounts[i][0], "error": str(r)})
        else:
            out.append(r)
    return out


def parse_args():
    p = argparse.ArgumentParser(description="CloakBrowser batch auto-login for the Flask OAuth app")
    p.add_argument("accounts", nargs="*", help="email:password pairs")
    p.add_argument("--batch", "-b", metavar="FILE", help="read email:password lines from FILE")
    p.add_argument("--headless", action="store_true", help="run browser headless")
    p.add_argument("--concurrent", "-c", type=int, default=1, help="concurrent sessions (max 3)")
    p.add_argument("--test", "-t", action="store_true", help="do not persist / skip-check")
    p.add_argument("--force", action="store_true", help="re-login accounts already in tokens.json")
    p.add_argument("--no-proxy", action="store_true", help="force direct even if proxies.txt exists")
    p.add_argument("--dot", action="store_true",
                   help="expand each Gmail account into dot-method variants "
                        "(all variants resolve to the SAME Google account)")
    p.add_argument("--count", type=int, default=5,
                   help="number of dot variants per account with --dot (default 5)")
    p.add_argument("--sessions", action="store_true",
                   help="persist each account's login in a profile folder so later "
                        "runs skip the password (recommended for headless/server)")
    p.add_argument("--session-base", metavar="DIR", default=SESSIONS_BASE,
                   help=f"base folder for persistent session profiles (default: {SESSIONS_BASE})")
    p.add_argument("--debug", "-d", action="store_true", help="verbose debug output")
    return p.parse_args()


async def main():
    global DEBUG
    args = parse_args()
    if args.debug:
        DEBUG = True
        os.environ["DEBUG"] = "true"

    if not server_healthy():
        log("App not running! Start it first: app/.venv/Scripts/python.exe app/main.py", "ERR")
        sys.exit(1)

    if args.batch:
        accounts = load_accounts(args.batch)
    else:
        accounts = [a for a in (parse_account(x) for x in args.accounts) if a]
    if not accounts:
        log("No accounts. Pass email:password or --batch accounts.txt", "ERR")
        sys.exit(1)

    if args.dot:
        non_gmail = [e for e, _ in accounts if e.rsplit("@", 1)[-1].lower() not in GMAIL_DOMAINS]
        if non_gmail:
            log(f"--dot only applies to Gmail; left unchanged: {', '.join(non_gmail)}", "WAIT")
        accounts = expand_dot_accounts(accounts, args.count)
        log(f"Dot method: expanded to {len(accounts)} variant(s). "
            f"All variants log into the SAME Google account (Gmail ignores dots).")
        if not args.test:
            log("Tip: run --dot with --test — variants collapse to one tokens.json "
                "entry (keyed by the canonical email Google returns).", "WAIT")

    if not args.test:
        before = len(accounts)
        accounts = filter_new(accounts, force=args.force)
        if before != len(accounts):
            log(f"Skipped {before - len(accounts)} account(s) already in tokens.json")
        if not accounts:
            log("All accounts already exist. Use --force to re-login.", "OK")
            sys.exit(0)

    proxies = [] if args.no_proxy else load_proxies()
    if proxies:
        note = " (rotating)" if len(proxies) >= len(accounts) else \
            f" (fewer than {len(accounts)} accounts — some IPs will repeat)"
        log(f"Proxy: {len(proxies)} loaded{note}")
    else:
        log("Proxy: none (direct connections)")

    session_base = args.session_base if args.sessions else None
    if session_base:
        log(f"Sessions: persistent profiles under {session_base} "
            f"(login saved per account; later runs skip the password)")

    concurrent = max(1, min(3, args.concurrent))
    log(f"Auto-login | {len(accounts)} account(s) | "
        f"{'HEADLESS' if args.headless else 'VISIBLE'} | concurrent={concurrent}")

    results = await run_batch(accounts, headless=args.headless,
                              concurrent=concurrent, test_only=args.test, proxies=proxies,
                              session_base=session_base)

    # Retry failures up to 3x.
    for attempt in range(1, 4):
        failed = [(r["email"]) for r in results if not r.get("success")]
        if not failed:
            break
        retry_accounts = [(e, p) for (e, p) in accounts if e in set(failed)]
        log(f"Retrying {len(retry_accounts)} failed (attempt {attempt}/3)...", "WAIT")
        await asyncio.sleep(3)
        retried = await run_batch(retry_accounts, headless=args.headless,
                                  concurrent=concurrent, test_only=args.test, proxies=proxies,
                                  session_base=session_base)
        by_email = {r["email"]: r for r in retried}
        results = [by_email.get(r["email"], r) if not r.get("success") else r for r in results]

    ok = sum(1 for r in results if r.get("success"))
    log(f"{'=' * 40}")
    for r in results:
        s = "OK" if r.get("success") else "FAIL"
        err = f" — {r.get('error')}" if r.get("error") else ""
        log(f"  [{s}] {r['email']}{err}")
    log(f"Final: {ok}/{len(results)} accounts active")


if __name__ == "__main__":
    asyncio.run(main())
