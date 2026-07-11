"""updater.py — in-app self-update from GitHub Releases.

Checks the repo's latest release, and if it's newer than this build, downloads
the new VideoPipelineStudio.exe and swaps it in (Windows can't overwrite a
running exe directly, so a tiny helper .bat replaces it and relaunches).
"""
import json, os, re, subprocess, sys, tempfile, urllib.request

# Public release channel (source repo is private, so the updater reads from here —
# no token needed, only the built exe is ever published to it).
REPO = "sahiljani/bulk-video-releases"
VERSION = "2.2"          # bump this each release; compared against the latest tag


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def latest_release(timeout=30):
    """Return (tag, exe_download_url, notes). Raises on network error."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "vps-updater"})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    tag = d.get("tag_name", "").lstrip("v")
    exe = next((a["browser_download_url"] for a in d.get("assets", [])
                if a["name"].lower().endswith(".exe")), None)
    return tag, exe, d.get("body", "")


def is_newer(remote_tag):
    return _ver_tuple(remote_tag) > _ver_tuple(VERSION)


def download(url, dest, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": "vps-updater"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", 0) or 0)
        got = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk); got += len(chunk)
            if progress and total:
                progress(got, total)
    return dest


def apply_and_restart(new_exe):
    """Replace the running exe with new_exe and relaunch (Windows)."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update only works on the packaged .exe build. "
                           "For the source checkout, use: git pull.")
    cur = sys.executable
    bat = os.path.join(tempfile.gettempdir(), "vps_update.bat")
    with open(bat, "w") as f:
        f.write("@echo off\r\n"
                "ping 127.0.0.1 -n 3 >nul\r\n"          # wait for this app to exit
                f'move /y "{new_exe}" "{cur}" >nul\r\n'
                f'start "" "{cur}"\r\n'
                'del "%~f0"\r\n')
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)  # CREATE_NO_WINDOW
    sys.exit(0)
