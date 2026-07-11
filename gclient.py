"""gclient.py — Google Cloud / Gemini access for the desktop app.

Auth is the gcloud CLI (user runs 'Login with Google Cloud' once). We take the
OAuth access token from `gcloud auth print-access-token` and call the Gemini
(Generative Language) API with it, billing/quota against the chosen project via
the x-goog-user-project header. No API key to paste.

Exposes: login(), logout(), account(), token(), list_projects(), list_models(),
test_model(), generate_script(), generate_image(), tts_wav().
"""
import base64, json, re, subprocess, sys, wave, urllib.request, urllib.error

GCLOUD = "gcloud.cmd" if sys.platform == "win32" else "gcloud"
REGIONS = ["us-central1", "us-east1", "us-east4", "us-west1", "us-west4",
           "europe-west1", "europe-west4", "asia-southeast1"]

# Sensible defaults if the live model list can't be fetched.
FALLBACK_MODELS = {
    "text":  ["gemini-2.5-pro", "gemini-2.5-flash"],
    "image": ["gemini-2.5-flash-image"],
    "tts":   ["gemini-2.5-pro-preview-tts", "gemini-2.5-flash-preview-tts"],
}
VOICES = ["Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr",
          "Autonoe", "Callirrhoe", "Enceladus", "Iapetus", "Umbriel", "Algieba"]


def _run(args, timeout=600):
    return subprocess.run([GCLOUD, *args], capture_output=True, text=True, timeout=timeout)


def account():
    try:
        a = _run(["config", "get-value", "account"], 15).stdout.strip()
        return "" if a in ("", "(unset)") else a
    except Exception:
        return ""


def token():
    r = _run(["auth", "print-access-token"], 30)
    if r.returncode != 0:
        raise RuntimeError("Not logged in. Click 'Login with Google Cloud' first.\n" + r.stderr[-300:])
    return r.stdout.strip()


# Copy-paste login: no browser is launched. We start gcloud in --no-launch-browser
# mode, hand the URL to the UI, and feed back the code the user pastes.
_login = {"proc": None, "out": ""}


def login_start():
    """Begin copy-paste login. Returns the sign-in URL for the user to open."""
    import re as _re, time as _t
    if _login["proc"] and _login["proc"].poll() is None:
        _login["proc"].kill()
    _login["out"] = ""
    p = subprocess.Popen([GCLOUD, "auth", "login", "--no-launch-browser"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    _login["proc"] = p
    deadline = _t.time() + 30
    while _t.time() < deadline:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                break
            continue
        _login["out"] += line
        m = _re.search(r"https://accounts\.google\.com\S+", line)
        if m:
            return m.group(0)
    raise RuntimeError("Could not get a login link from gcloud. Is the Google Cloud SDK installed?\n"
                       + _login["out"][-300:])


def login_finish(code):
    """Feed the pasted authorization code to gcloud; return the signed-in account."""
    p = _login["proc"]
    if not p or p.poll() is not None:
        return account()
    try:
        p.stdin.write(code.strip() + "\n"); p.stdin.flush()
        p.wait(timeout=90)
    except Exception:
        pass
    return account()


def login():
    """Fallback: browser-based login (blocks). The UI uses login_start/finish."""
    r = _run(["auth", "login", "--quiet"], 600)
    if r.returncode != 0:
        raise RuntimeError("gcloud login failed:\n" + (r.stderr or r.stdout)[-400:])
    return account()


def logout():
    _run(["auth", "revoke", "--all", "--quiet"], 60)


def list_projects():
    r = _run(["projects", "list", "--format=value(projectId)", "--limit=200"], 60)
    return [p for p in r.stdout.split() if p] if r.returncode == 0 else []


class Gemini:
    """Talks to Vertex AI regional endpoints — works with the gcloud user token
    (cloud-platform scope), the same path the yt-system factory uses."""
    def __init__(self, project, region="us-central1"):
        self.project = project
        self.region = region

    def _url(self, model, action):
        return (f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{self.project}"
                f"/locations/{self.region}/publishers/google/models/{model}:{action}")

    def _post(self, url, payload, timeout=300):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
              headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            m = re.search(r'"message":\s*"([^"]+)"', body)
            raise RuntimeError(f"HTTP {e.code}: {m.group(1) if m else body[:300]}") from None

    # ---- models ----------------------------------------------------------
    def list_models(self):
        """Best-effort live listing from Vertex Model Garden, curated fallback.
        (Model names are stable; the real 'is it usable' check is Test below.)"""
        try:
            url = (f"https://{self.region}-aiplatform.googleapis.com/v1beta1/publishers/"
                   f"google/models?pageSize=200")
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
            out = {"text": [], "image": [], "tts": []}
            for m in data.get("publisherModels", []):
                name = m.get("name", "").split("/")[-1]
                if not name.startswith("gemini"):
                    continue
                bucket = "tts" if "tts" in name else "image" if "image" in name else "text"
                out[bucket].append(name)
            for k in out:
                out[k] = sorted(set(out[k])) or list(FALLBACK_MODELS[k])
            return out
        except Exception:
            return {k: list(v) for k, v in FALLBACK_MODELS.items()}

    def test_model(self, model):
        """Real call — returns (ok, message). A suspended project or missing
        model surfaces here (e.g. 'Consumer ... has been suspended')."""
        try:
            self._post(self._url(model, "generateContent"),
                       {"contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}]},
                       timeout=40)
            return True, f"OK — reachable on {self.project}/{self.region}"
        except Exception as e:
            return False, str(e)

    # ---- generation ------------------------------------------------------
    def generate_script(self, model, prompt):
        resp = self._post(self._url(model, "generateContent"),
                          {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json",
                                                "temperature": 0.9}})
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)

    def generate_image(self, model, prompt, dest):
        resp = self._post(self._url(model, "generateContent"),
                          {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}})
        for cand in resp.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and str(inline.get("mimeType", "")).startswith("image"):
                    open(dest, "wb").write(base64.b64decode(inline["data"]))
                    return
        raise RuntimeError("No image returned by " + model)

    def tts_wav(self, model, voice, text, dest):
        resp = self._post(self._url(model, "generateContent"),
                          {"contents": [{"role": "user", "parts": [{"text": text}]}],
                           "generationConfig": {"responseModalities": ["AUDIO"],
                               "speechConfig": {"voiceConfig": {
                                   "prebuiltVoiceConfig": {"voiceName": voice}}}}})
        part = resp["candidates"][0]["content"]["parts"][0]
        inline = part.get("inlineData") or part.get("inline_data")
        if not inline:
            raise RuntimeError("No audio returned by " + model)
        pcm = base64.b64decode(inline["data"])
        rate = 24000
        m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
        if m:
            rate = int(m.group(1))
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
        return dest
