"""dola.py — generate videos on dola.com by driving a real Chrome window.

Dola has no public API, so (exactly like the app/ automation) we open a browser,
log in with Google once (the session persists in a profile folder so you only do
it once), type the prompt, wait for the render, and download the .mp4.

Uses Playwright's SYNC api — safe here because the app always calls this from a
background worker thread, never the Tk main thread. A single DolaSession keeps
one browser open and can generate many videos back-to-back (single or batch, and
as the video step of the full pipeline).
"""
import os, shutil, subprocess, sys, time, urllib.request

CHAT_URL = "https://www.dola.com/chat/"


def _browsers_dir():
    """A PERSISTENT folder for Playwright's browser, outside PyInstaller's temp
    _MEI dir (which is wiped on exit — that's why the bundled path failed)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.path.expanduser("~/.cache")
    return os.path.join(base, "VideoPipelineStudio", "ms-playwright")


# Must be set BEFORE playwright is imported (it is imported lazily below).
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _browsers_dir())


def _driver_install(log):
    """Run 'playwright install chromium' correctly even from a frozen exe, where
    `sys.executable -m playwright` does NOT work (sys.executable is the app)."""
    os.makedirs(os.environ["PLAYWRIGHT_BROWSERS_PATH"], exist_ok=True)
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        drv = compute_driver_executable()
        args = list(drv) if isinstance(drv, (list, tuple)) else [drv]
        env = get_driver_env()
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        r = subprocess.run([*args, "install", "chromium"], env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            log("browser install issue: " + ((r.stderr or r.stdout) or "")[-300:])
    except Exception:
        # dev fallback (running from source, not the exe)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)


def ensure_chromium(log=print):
    """First-run: download the Chromium build Playwright needs (~150MB), into the
    persistent browsers dir. No-op once installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed. Run:  pip install playwright")
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        if exe and os.path.exists(exe):
            return
    except Exception:
        pass
    log("First run: downloading the browser for Dola (one-time, ~150MB)…")
    _driver_install(log)
    # verify it actually landed
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            if not (p.chromium.executable_path and os.path.exists(p.chromium.executable_path)):
                raise RuntimeError("browser still missing")
        log("Browser ready.")
    except Exception as e:
        raise RuntimeError("Could not install the Dola browser automatically. "
                           "Close the app and run once in a terminal:  playwright install chromium\n"
                           f"({e})")


class DolaSession:
    def __init__(self, profile_dir, download_dir, headless=False, log=print):
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        self.headless = headless
        self.log = log
        self._pw = None
        self.ctx = None
        self.page = None
        os.makedirs(download_dir, exist_ok=True)
        os.makedirs(profile_dir, exist_ok=True)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self.ctx:
            return
        ensure_chromium(self.log)
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir, headless=self.headless, accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800})
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.ctx = self.page = self._pw = None

    def is_logged_in(self):
        """True when no 'Log In' button is showing on the chat page."""
        self.start()
        try:
            btn = self.page.locator('button:has-text("Log In"), div.cursor-pointer:has-text("Log In")').first
            return not (btn.count() > 0 and btn.is_visible())
        except Exception:
            return False

    def login(self):
        """Click Log In → Continue with Google. The user completes it in the
        visible window (that's why headless is off by default)."""
        self.start()
        p = self.page
        btn = p.locator('button:has-text("Log In"), div.cursor-pointer:has-text("Log In")').first
        if btn.count() > 0 and btn.is_visible():
            btn.click(force=True); time.sleep(2)
            g = p.locator('button:has-text("Continue with Google"), '
                          '[class*="button-"]:has-text("Continue with Google")').first
            if g.count() > 0 and g.is_visible():
                g.click(force=True)
        self.log("Complete the Google sign-in in the Chrome window, then click 'I'm logged in'.")

    # ---- generation ------------------------------------------------------
    def generate(self, prompt, out_path, timeout=360):
        """Generate ONE video for `prompt`, save to out_path. Returns out_path or raises."""
        self.start()
        p = self.page
        if "dola.com" not in p.url:
            p.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000); time.sleep(4)

        cv = p.locator('button:has-text("Create Video")').first
        if cv.count() > 0:
            cv.click(force=True); self.log("Clicked 'Create Video'."); time.sleep(2)

        ta = p.locator('textarea, [contenteditable="true"]').first
        if ta.count() == 0:
            raise RuntimeError("Dola prompt box not found — is the page loaded / logged in?")
        ta.click(); time.sleep(0.4); ta.fill(prompt); time.sleep(1)
        self.log(f"Submitting prompt: {prompt[:60]}…")
        ta.press("Enter"); time.sleep(1)
        send = p.locator('button[type="submit"], button:has(svg), div.cursor-pointer:has(svg)').last
        if send.count() > 0 and send.is_visible():
            try: send.click(force=True)
            except Exception: pass

        time.sleep(2)
        if p.evaluate("() => document.body.innerText.includes('not available for guests')"):
            raise RuntimeError("Dola says video is not available for guests — log in first.")

        # capture mp4 URLs from network as they fly by
        captured = []
        p.on("response", lambda r: captured.append(r.url)
             if (".mp4" in r.url or "cloudfront" in r.url or "amazonaws.com/video" in r.url)
             and r.url not in captured else None)

        self.log("Waiting for Dola to render (30–120s typical)…")
        video_url, t0 = None, time.time()
        while time.time() - t0 < timeout:
            time.sleep(4)
            for u in captured:
                if u.startswith("http") and ("mp4" in u or "video" in u):
                    video_url = u; break
            if not video_url:
                try:
                    card = p.locator('[class*="block-video"], [class*="video-player"]').first
                    if card.count() > 0:
                        card.click(force=True); time.sleep(2)
                except Exception:
                    pass
                srcs = p.evaluate("""() => Array.from(document.querySelectorAll('video'))
                    .map(v => v.src || (v.querySelector('source')?v.querySelector('source').src:''))
                    .filter(s => s && s.length > 5)""")
                if srcs:
                    video_url = srcs[0]
            if not video_url:
                links = p.evaluate("""() => Array.from(document.querySelectorAll('a[href*=".mp4"],a[download]'))
                    .map(l => l.href||'').filter(h => h.includes('http'))""")
                if links:
                    video_url = links[0]
            if video_url:
                break
            el = int(time.time() - t0)
            if el and el % 20 == 0:
                self.log(f"  …still rendering ({el}s)")

        if not video_url:
            raise RuntimeError(f"Dola timed out after {timeout}s with no video URL.")
        self.log(f"Render done: {video_url[:70]}… downloading")

        try:
            req = urllib.request.Request(video_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(out_path, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception:
            # fall back to a real browser download
            with p.expect_download(timeout=30000) as di:
                p.evaluate("""(u) => { const a=document.createElement('a');
                    a.href=u; a.download='v.mp4'; document.body.appendChild(a); a.click(); a.remove(); }""",
                    video_url)
            di.value.save_as(out_path)
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
            raise RuntimeError("Downloaded Dola video is empty.")
        self.log(f"Saved {os.path.basename(out_path)} ({os.path.getsize(out_path)//1024} KB)")
        return out_path
