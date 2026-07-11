#!/usr/bin/env python3
"""
Video Pipeline Studio
=====================
Interactive desktop app (Windows / Mac / Linux) that runs a full
text-to-video production pipeline on the Gemini API:

  1. Topic  -> full script broken into scenes (narration + image prompt
               + video motion prompt) via Gemini 2.5 Pro
  2. Scene  -> image        (Gemini image model)
  3. Scene  -> speech (WAV) (Gemini 2.5 Pro TTS)
  4. Scene  -> video        (Veo image-to-video, scene image = first frame)
  5. ffmpeg -> sync each video to its narration length, mux audio,
               concat everything into final.mp4

Everything is saved into a local project directory. Failed steps are
tracked per scene and can be retried / regenerated individually or in
bulk from the UI.

Requirements:
  * Python 3.9+  (Tkinter is included in the standard Windows installer)
  * ffmpeg + ffprobe on PATH  (https://ffmpeg.org  /  `winget install ffmpeg`)
  * A Google AI Studio API key (https://aistudio.google.com/apikey)

No third-party Python packages are required - only the standard library.
"""

import base64
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

API_BASE = "https://generativelanguage.googleapis.com"
CURRENT_VERSION = "1.0.0"
UPDATE_URL = "https://example.com/version.json" # Change this! Expected format: {"version": "1.0.1", "url": "https://..."}

DEFAULTS = {
    "script_model": "gemini-2.5-pro",
    "image_model": "gemini-2.5-flash-image",
    "tts_model": "gemini-2.5-pro-preview-tts",
    "video_model": "veo-3.0-generate-001",
    "voice": "Kore",
    "aspect_ratio": "16:9",
    "num_scenes": "6",
    "style": "cinematic, photorealistic, dramatic lighting, high detail",
}

TTS_VOICES = ["Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus",
              "Zephyr", "Autonoe", "Callirrhoe", "Enceladus", "Iapetus"]

SCRIPT_PROMPT = """You are a professional video director and screenwriter.
Write a video script about the topic below, split into exactly {n} scenes.

Topic: {topic}

Visual style for the whole video: {style}

Return ONLY valid JSON matching this schema:
{{
  "title": "...",
  "scenes": [
    {{
      "narration": "2-4 spoken sentences of voiceover for this scene",
      "image_prompt": "A richly detailed prompt for a text-to-image model describing the KEY FRAME of this scene. Include subject, composition, camera angle, lens, lighting, mood, color palette, and the style '{style}'. No text/captions in the image.",
      "video_prompt": "A prompt for an image-to-video model that animates that exact frame: describe camera motion (dolly/pan/zoom), subject movement, atmosphere, pacing. Keep visual continuity with the image."
    }}
  ]
}}
Scenes must flow as one continuous story. Narration must sound natural when read aloud."""


# --------------------------------------------------------------------------
# Gemini REST client (stdlib only)
# --------------------------------------------------------------------------

class GeminiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request(self, method: str, url: str, payload=None, timeout=300):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-goog-api-key", self.api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:800]}") from None

    def post(self, path: str, payload: dict, timeout=300) -> dict:
        return self._request("POST", f"{API_BASE}{path}", payload, timeout)

    def get(self, path: str, timeout=60) -> dict:
        return self._request("GET", f"{API_BASE}{path}", None, timeout)

    def download(self, url: str, dest: Path):
        req = urllib.request.Request(url)
        req.add_header("x-goog-api-key", self.api_key)
        with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)

    # ---- pipeline calls ---------------------------------------------------

    def generate_script(self, model, topic, n_scenes, style) -> dict:
        payload = {
            "contents": [{"parts": [{"text": SCRIPT_PROMPT.format(
                topic=topic, n=n_scenes, style=style)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.9},
        }
        resp = self.post(f"/v1beta/models/{model}:generateContent", payload)
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
        # tolerate accidental markdown fences
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        data = json.loads(text)
        if not data.get("scenes"):
            raise RuntimeError("Model returned no scenes")
        return data

    def generate_image(self, model, prompt, dest: Path):
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
        resp = self.post(f"/v1beta/models/{model}:generateContent", payload)
        for cand in resp.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("mimeType", "").startswith("image"):
                    dest.write_bytes(base64.b64decode(inline["data"]))
                    return
        raise RuntimeError(f"No image in response: {json.dumps(resp)[:400]}")

    def generate_tts(self, model, voice, text, dest: Path):
        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}}},
            },
        }
        resp = self.post(f"/v1beta/models/{model}:generateContent", payload)
        part = resp["candidates"][0]["content"]["parts"][0]
        inline = part.get("inlineData") or part.get("inline_data")
        if not inline:
            raise RuntimeError(f"No audio in response: {json.dumps(resp)[:400]}")
        pcm = base64.b64decode(inline["data"])
        rate = 24000
        m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
        if m:
            rate = int(m.group(1))
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)

    def generate_video(self, model, prompt, image_path: Path, aspect,
                       dest: Path, log=lambda s: None):
        instance = {"prompt": prompt}
        if image_path and image_path.exists():
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(image_path.read_bytes()).decode(),
                "mimeType": "image/png",
            }
        payload = {"instances": [instance],
                   "parameters": {"aspectRatio": aspect}}
        resp = self.post(f"/v1beta/models/{model}:predictLongRunning", payload)
        op_name = resp["name"]
        log(f"    Veo operation started: {op_name}")
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(10)
            op = self.get(f"/v1beta/{op_name}")
            if op.get("error"):
                raise RuntimeError(f"Veo error: {op['error']}")
            if op.get("done"):
                r = op.get("response", {})
                gvr = r.get("generateVideoResponse", r)
                samples = (gvr.get("generatedSamples")
                           or gvr.get("generated_samples")
                           or gvr.get("videos") or [])
                if not samples:
                    raise RuntimeError(f"Veo finished with no video: {json.dumps(r)[:400]}")
                s = samples[0]
                video = s.get("video", s)
                uri = video.get("uri")
                if uri:
                    self.download(uri, dest)
                elif video.get("bytesBase64Encoded"):
                    dest.write_bytes(base64.b64decode(video["bytesBase64Encoded"]))
                else:
                    raise RuntimeError(f"Unrecognized video payload: {json.dumps(s)[:400]}")
                return
            log("    ...rendering video")
        raise RuntimeError("Veo operation timed out after 15 min")


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------

def run_cmd(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed:\n{p.stderr[-800:]}")
    return p.stdout


def media_duration(path: Path) -> float:
    out = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())


def build_scene_clip(video: Path, audio: Path, dest: Path):
    """Make one clip whose length == narration length.
    If the narration is longer than the video, the video loops; if shorter,
    the video is trimmed. Audio and video always end together."""
    adur = media_duration(audio)
    run_cmd([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{adur:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(dest),
    ])


def concat_clips(clips, dest: Path, workdir: Path):
    lst = workdir / "concat.txt"
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(dest)])


# --------------------------------------------------------------------------
# Project state
# --------------------------------------------------------------------------

STEPS = ("image", "audio", "video")


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.file = root / "project.json"
        self.data = {"topic": "", "title": "", "scenes": []}

    def load(self):
        if self.file.exists():
            self.data = json.loads(self.file.read_text(encoding="utf-8"))

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def scene_paths(self, i):
        s = self.root / "scenes"
        s.mkdir(parents=True, exist_ok=True)
        return {"image": s / f"scene_{i+1:02d}.png",
                "audio": s / f"scene_{i+1:02d}.wav",
                "video": s / f"scene_{i+1:02d}.mp4",
                "clip": s / f"scene_{i+1:02d}_final.mp4"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Pipeline Studio")
        self.geometry("1180x780")
        self.minsize(980, 640)

        self.project = None
        self.worker = None
        self.stop_flag = threading.Event()
        self.msg_q = queue.Queue()

        self._build_ui()
        self.after(100, self._poll_queue)
        self.after(1000, lambda: self._run_bg(self._check_for_updates))

    # ---- layout -----------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}

        top = ttk.LabelFrame(self, text="Settings")
        top.pack(fill="x", padx=8, pady=6)

        self.api_key = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        self.out_dir = tk.StringVar(value=str(Path.home() / "VideoPipelineProjects" / "project1"))
        self.topic = tk.StringVar()
        self.vars = {k: tk.StringVar(value=v) for k, v in DEFAULTS.items()}

        r1 = ttk.Frame(top); r1.pack(fill="x")
        ttk.Label(r1, text="Gemini API key:").pack(side="left", **pad)
        ttk.Entry(r1, textvariable=self.api_key, show="*", width=42).pack(side="left", **pad)
        ttk.Label(r1, text="Project folder:").pack(side="left", **pad)
        ttk.Entry(r1, textvariable=self.out_dir, width=48).pack(side="left", fill="x", expand=True, **pad)
        ttk.Button(r1, text="Browse…", command=self._browse).pack(side="left", **pad)

        r2 = ttk.Frame(top); r2.pack(fill="x")
        ttk.Label(r2, text="Topic / idea:").pack(side="left", **pad)
        ttk.Entry(r2, textvariable=self.topic).pack(side="left", fill="x", expand=True, **pad)
        ttk.Label(r2, text="Scenes:").pack(side="left", **pad)
        ttk.Spinbox(r2, from_=1, to=30, textvariable=self.vars["num_scenes"], width=4).pack(side="left", **pad)

        r3 = ttk.Frame(top); r3.pack(fill="x")
        ttk.Label(r3, text="Style:").pack(side="left", **pad)
        ttk.Entry(r3, textvariable=self.vars["style"], width=52).pack(side="left", **pad)
        ttk.Label(r3, text="Voice:").pack(side="left", **pad)
        ttk.Combobox(r3, textvariable=self.vars["voice"], values=TTS_VOICES, width=12).pack(side="left", **pad)
        ttk.Label(r3, text="Aspect:").pack(side="left", **pad)
        ttk.Combobox(r3, textvariable=self.vars["aspect_ratio"], values=["16:9", "9:16"], width=6).pack(side="left", **pad)

        r4 = ttk.Frame(top); r4.pack(fill="x")
        for label, key, width in (("Script model:", "script_model", 18),
                                  ("Image model:", "image_model", 24),
                                  ("TTS model:", "tts_model", 26),
                                  ("Video model:", "video_model", 22)):
            ttk.Label(r4, text=label).pack(side="left", **pad)
            if key == "video_model":
                ttk.Combobox(r4, textvariable=self.vars[key], values=["veo-3.0-generate-001", "dola"], width=width).pack(side="left", **pad)
            else:
                ttk.Entry(r4, textvariable=self.vars[key], width=width).pack(side="left", **pad)

        r5 = ttk.Frame(top); r5.pack(fill="x", pady=(2, 0))
        ttk.Label(r5, text="Dola Accounts:\n(email:pass:totp)").pack(side="left", anchor="n", **pad)
        self.dola_accounts_box = scrolledtext.ScrolledText(r5, height=3, width=80)
        self.dola_accounts_box.pack(side="left", fill="x", expand=True, **pad)
        try:
            accs = Path("/home/azureuser/bulk-Video-generation/app/accounts.txt").read_text()
            self.dola_accounts_box.insert("1.0", accs)
        except Exception:
            pass

        # buttons
        btns = ttk.Frame(self); btns.pack(fill="x", padx=8)
        self.buttons = {}
        for text, cmd in (
            ("1. Generate Script", self.act_script),
            ("2. Generate Images", lambda: self.act_step("image")),
            ("3. Generate Speech", lambda: self.act_step("audio")),
            ("4. Generate Videos", lambda: self.act_step("video")),
            ("5. Merge Final Video", self.act_merge),
            ("▶ Run Full Pipeline", self.act_full),
            ("⟳ Retry Failed", self.act_retry),
        ):
            b = ttk.Button(btns, text=text, command=cmd)
            b.pack(side="left", padx=4, pady=6)
            self.buttons[text] = b
        ttk.Button(btns, text="■ Stop", command=self.stop_flag.set).pack(side="left", padx=4)
        ttk.Button(btns, text="Open Folder", command=self._open_folder).pack(side="right", padx=4)
        ttk.Button(btns, text="Load Project", command=self.act_load).pack(side="right", padx=4)

        # scene table
        mid = ttk.Frame(self); mid.pack(fill="both", expand=True, padx=8)
        cols = ("scene", "narration", "image", "audio", "video")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        widths = {"scene": 55, "narration": 560, "image": 90, "audio": 90, "video": 90}
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=widths[c], anchor="w" if c == "narration" else "center")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("failed", background="#5a2020", foreground="white")
        self.tree.tag_configure("done", background="#1f4d2b", foreground="white")

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Regenerate image", command=lambda: self._regen_selected("image"))
        menu.add_command(label="Regenerate speech", command=lambda: self._regen_selected("audio"))
        menu.add_command(label="Regenerate video", command=lambda: self._regen_selected("video"))
        menu.add_separator()
        menu.add_command(label="Edit prompts / narration", command=self._edit_selected)
        self.tree.bind("<Button-3>", lambda e: (self.tree.selection_set(
            self.tree.identify_row(e.y)) if self.tree.identify_row(e.y) else None,
            menu.tk_popup(e.x_root, e.y_root)))

        # progress + log
        bottom = ttk.Frame(self); bottom.pack(fill="both", padx=8, pady=6)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 4))
        self.log_box = scrolledtext.ScrolledText(bottom, height=9, state="disabled",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)

    # ---- helpers ----------------------------------------------------------

    def log(self, msg):
        self.msg_q.put(("log", msg))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("[%H:%M:%S] ") + payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "refresh":
                    self._refresh_table()
                elif kind == "progress":
                    done, total = payload
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                elif kind == "error":
                    messagebox.showerror("Error", payload)
                elif kind == "update_ready":
                    bat_path, ver = payload
                    if messagebox.askyesno("Update Ready", f"Version {ver} is ready to install. Restart now?"):
                        subprocess.Popen(bat_path, shell=True)
                        self.destroy()
                        sys.exit(0)
                elif kind == "update_ready_py":
                    ver = payload
                    messagebox.showinfo("Update Complete", f"Successfully updated script to {ver}. Please restart the application.")
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _check_for_updates(self):
        try:
            req = urllib.request.Request(UPDATE_URL)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            latest_version = data.get("version", CURRENT_VERSION)
            download_url = data.get("url", "")
            
            def parse_v(v): return [int(x) for x in v.split(".") if x.isdigit()]
            
            if parse_v(latest_version) > parse_v(CURRENT_VERSION) and download_url:
                self.log(f"New version {latest_version} is available! Downloading...")
                self._apply_update(download_url, latest_version)
        except Exception as e:
            self.log(f"Auto-update check skipped or failed: {e}")

    def _apply_update(self, download_url, latest_version):
        try:
            import tempfile
            is_exe = getattr(sys, 'frozen', False)
            ext = ".exe" if is_exe else ".py"
            
            req = urllib.request.Request(download_url)
            tmp_path = Path(tempfile.gettempdir()) / f"update_new{ext}"
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
                
            self.log("Download complete.")
            
            if is_exe:
                bat_path = Path(tempfile.gettempdir()) / "updater.bat"
                current_exe = Path(sys.executable).resolve()
                bat_script = f'@echo off\ntimeout /t 2 /nobreak >nul\nmove /y "{tmp_path}" "{current_exe}"\nstart "" "{current_exe}"\ndel "%~f0"\n'
                bat_path.write_text(bat_script)
                self.msg_q.put(("update_ready", (str(bat_path), latest_version)))
            else:
                current_script = Path(__file__).resolve()
                shutil.move(tmp_path, current_script)
                self.msg_q.put(("update_ready_py", latest_version))
                
        except Exception as e:
            self.log(f"Failed to apply update: {e}")

    def _browse(self):
        d = filedialog.askdirectory()
        if d:
            self.out_dir.set(d)

    def _open_folder(self):
        p = self.out_dir.get()
        if sys.platform == "win32":
            os.startfile(p)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])

    def _client(self):
        key = self.api_key.get().strip()
        if not key:
            raise RuntimeError("Enter your Gemini API key first.")
        return GeminiClient(key)

    def _ensure_project(self) -> Project:
        if self.project is None or str(self.project.root) != self.out_dir.get():
            self.project = Project(Path(self.out_dir.get()))
            self.project.load()
        return self.project

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        if not self.project:
            return
        for i, sc in enumerate(self.project.data["scenes"]):
            st = sc.setdefault("status", {})
            vals = (i + 1, sc["narration"][:90],
                    *(st.get(k, "—") for k in STEPS))
            statuses = [st.get(k) for k in STEPS]
            tag = ()
            if "failed" in statuses:
                tag = ("failed",)
            elif all(s == "done" for s in statuses):
                tag = ("done",)
            self.tree.insert("", "end", iid=str(i), values=vals, tags=tag)

    def _run_bg(self, fn, *args):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A job is already running. Stop it first.")
            return
        self.stop_flag.clear()

        def wrapper():
            try:
                fn(*args)
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.msg_q.put(("error", str(e)))
                traceback.print_exc()
            finally:
                self.msg_q.put(("refresh", None))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    # ---- actions ----------------------------------------------------------

    def act_load(self):
        self.project = None
        p = self._ensure_project()
        self.topic.set(p.data.get("topic", self.topic.get()))
        self._refresh_table()
        self.log(f"Loaded project: {p.root} ({len(p.data['scenes'])} scenes)")

    def act_script(self):
        topic = self.topic.get().strip()
        if not topic:
            messagebox.showwarning("Missing topic", "Enter a topic / idea first.")
            return
        self._run_bg(self._job_script, topic)

    def _job_script(self, topic):
        client = self._client()
        p = self._ensure_project()
        self.log(f"Generating script for: {topic}")
        data = client.generate_script(self.vars["script_model"].get(), topic,
                                      int(self.vars["num_scenes"].get()),
                                      self.vars["style"].get())
        p.data["topic"] = topic
        p.data["title"] = data.get("title", topic)
        p.data["scenes"] = [{**sc, "status": {}} for sc in data["scenes"]]
        p.save()
        (p.root / "script.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log(f"Script ready: \"{p.data['title']}\" with {len(p.data['scenes'])} scenes. Saved to script.json")
        self.msg_q.put(("refresh", None))

    def act_step(self, step, only_indices=None, only_failed=False):
        self._run_bg(self._job_step, step, only_indices, only_failed)

    def _job_step(self, step, only_indices, only_failed):
        client = self._client()
        p = self._ensure_project()
        scenes = p.data["scenes"]
        if not scenes:
            raise RuntimeError("No script yet - run step 1 first.")
        todo = []
        for i, sc in enumerate(scenes):
            st = sc.setdefault("status", {})
            if only_indices is not None and i not in only_indices:
                continue
            if only_failed and st.get(step) != "failed":
                continue
            if only_indices is None and not only_failed and st.get(step) == "done":
                continue
            todo.append(i)
        if not todo:
            self.log(f"[{step}] nothing to do (all done).")
            return
        self.msg_q.put(("progress", (0, len(todo))))
        for n, i in enumerate(todo):
            if self.stop_flag.is_set():
                self.log("Stopped by user.")
                return
            sc = scenes[i]
            paths = p.scene_paths(i)
            sc["status"][step] = "running"
            self.msg_q.put(("refresh", None))
            self.log(f"[{step}] scene {i+1}/{len(scenes)} ...")
            try:
                if step == "image":
                    client.generate_image(self.vars["image_model"].get(),
                                          sc["image_prompt"], paths["image"])
                elif step == "audio":
                    client.generate_tts(self.vars["tts_model"].get(),
                                        self.vars["voice"].get(),
                                        sc["narration"], paths["audio"])
                elif step == "video":
                    if not paths["image"].exists():
                        raise RuntimeError("scene image missing - generate images first")
                    
                    if self.vars["video_model"].get().lower() == "dola":
                        self._generate_video_dola(sc["video_prompt"], paths["video"])
                    else:
                        client.generate_video(self.vars["video_model"].get(),
                                              sc["video_prompt"], paths["image"],
                                              self.vars["aspect_ratio"].get(),
                                              paths["video"], log=self.log)
                sc["status"][step] = "done"
                self.log(f"[{step}] scene {i+1} ✓ -> {paths[step].name}")
            except Exception as e:
                sc["status"][step] = "failed"
                sc.setdefault("errors", {})[step] = str(e)
                self.log(f"[{step}] scene {i+1} ✗ {e}")
            p.save()
            self.msg_q.put(("refresh", None))
            self.msg_q.put(("progress", (n + 1, len(todo))))
        fails = sum(1 for i in todo if scenes[i]["status"].get(step) == "failed")
        self.log(f"[{step}] finished: {len(todo)-fails} ok, {fails} failed."
                 + (" Use '⟳ Retry Failed' to retry." if fails else ""))

    def act_retry(self):
        self._run_bg(self._job_retry)

    def _job_retry(self):
        for step in STEPS:
            if self.stop_flag.is_set():
                return
            self._job_step(step, None, only_failed=True)

    def act_full(self):
        topic = self.topic.get().strip()
        self._run_bg(self._job_full, topic)

    def _job_full(self, topic):
        p = self._ensure_project()
        if not p.data["scenes"]:
            if not topic:
                raise RuntimeError("Enter a topic first.")
            self._job_script(topic)
        for step in STEPS:
            if self.stop_flag.is_set():
                return
            self._job_step(step, None, False)
        if self.stop_flag.is_set():
            return
        self._job_merge()

    def act_merge(self):
        self._run_bg(self._job_merge)

    def _job_merge(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("ffmpeg/ffprobe not found on PATH. Install ffmpeg first "
                               "(Windows: `winget install ffmpeg`, then restart the app).")
        p = self._ensure_project()
        scenes = p.data["scenes"]
        clips = []
        self.msg_q.put(("progress", (0, len(scenes) + 1)))
        for i, sc in enumerate(scenes):
            if self.stop_flag.is_set():
                return
            paths = p.scene_paths(i)
            if not paths["video"].exists() or not paths["audio"].exists():
                raise RuntimeError(f"Scene {i+1} is missing its video or audio - "
                                   "generate/retry those steps first.")
            self.log(f"[merge] syncing scene {i+1} video length to narration...")
            build_scene_clip(paths["video"], paths["audio"], paths["clip"])
            clips.append(paths["clip"])
            self.msg_q.put(("progress", (i + 1, len(scenes) + 1)))
        final = p.root / "final.mp4"
        self.log("[merge] concatenating all scenes...")
        concat_clips(clips, final, p.root)
        self.msg_q.put(("progress", (len(scenes) + 1, len(scenes) + 1)))
        self.log(f"DONE ✔  Final video: {final}  ({media_duration(final):.1f}s)")

    def _generate_video_dola(self, prompt, dest_path):
        accounts_text = self.dola_accounts_box.get("1.0", "end").strip()
        import asyncio
        asyncio.run(self._async_generate_video_dola(prompt, dest_path, accounts_text))

    async def _async_generate_video_dola(self, prompt, dest_path, accounts_text):
        import sys, os, shutil, random, glob
        import full_lifecycle_video as flv
        from autologin import automate_google_login
        from cloakbrowser import launch_persistent_context_async
        import asyncio

        accounts = []
        for line in accounts_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            parts = line.split(":")
            if len(parts) >= 2:
                accounts.append(parts)

        if not accounts:
            raise RuntimeError("No accounts provided in the UI for Dola generation.")
        
        acc = random.choice(accounts)
        email, password = acc[0], acc[1]
        totp_secret = acc[2] if len(acc) > 2 else None

        self.log(f"[dola] Picked account {email} for generation.")

        if not flv.ensure_socks_proxy():
            raise RuntimeError("Cannot start SOCKS proxy for VPN routing.")

        vpn_proxy = {"server": flv.SPAIN_SOCKS_PROXY}
        session_dir = f"/home/azureuser/bulk-Video-generation/app/sessions/{email.replace('@', '_').replace('.', '_')}"

        flv.cleanup_zombie_chrome(session_dir)
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
            self.log("[dola] Cleared old session.")

        vpn_ok, vpn_ip = await flv.rotate_vpn_with_retry(max_retries=3)
        if not vpn_ok:
            raise RuntimeError("Failed to acquire verified Spain IP.")

        flv.restart_socks_proxy()

        self.log("[dola] Launching CloakBrowser...")
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=False, proxy=vpn_proxy,
            viewport={"width": 1280, "height": 900}, locale="en-US", humanize=True)
        page = await ctx.new_page()

        try:
            oauth_url2 = await flv.get_google_auth_url(ctx, page)
            if oauth_url2 == "GEO_BLOCKED" or not oauth_url2:
                raise RuntimeError("Failed to get Google Auth URL. Geo-blocked?")

            await ctx.clear_cookies()
            self.log("[dola] Authenticating with Google...")
            state2 = await automate_google_login(
                oauth_url2, email, password, headless=False, proxy=vpn_proxy,
                session_dir=session_dir, existing_ctx=ctx, existing_page=page,
                close_on_finish=False, totp_secret=totp_secret)
            
            if not state2.get("login_done", False):
                raise RuntimeError("Google login failed.")

            self.log("[dola] Requesting video generation...")
            if not flv.page_alive(page):
                page = await ctx.new_page()
                await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(5)

            video_result = await flv.generate_and_download_video(page, prompt)

            if video_result == "SUCCESS":
                mp4s = glob.glob(os.path.join(flv.DOWNLOAD_DIR, "*.mp4"))
                if mp4s:
                    video_path = max(mp4s, key=os.path.getmtime)
                    shutil.move(video_path, str(dest_path))
                    self.log(f"[dola] Video generated and moved to {dest_path.name}")
                else:
                    raise RuntimeError("Video marked SUCCESS but no mp4 found in DOWNLOAD_DIR.")
            else:
                raise RuntimeError(f"Dola video generation failed: {video_result}")

        finally:
            self.log("[dola] Cleaning up account...")
            try:
                if flv.page_alive(page):
                    await flv.delete_dola_account(page)
            except Exception as e:
                self.log(f"[dola] Warning during account deletion: {e}")
            try:
                if ctx:
                    await ctx.close()
            except Exception:
                pass
            flv.cleanup_zombie_chrome(session_dir)

    # ---- per-scene actions -------------------------------------------------

    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _regen_selected(self, step):
        i = self._selected_index()
        if i is not None:
            self.act_step(step, only_indices={i})

    def _edit_selected(self):
        i = self._selected_index()
        if i is None or not self.project:
            return
        sc = self.project.data["scenes"][i]
        win = tk.Toplevel(self)
        win.title(f"Edit scene {i+1}")
        win.geometry("720x520")
        boxes = {}
        for field in ("narration", "image_prompt", "video_prompt"):
            ttk.Label(win, text=field).pack(anchor="w", padx=8, pady=(8, 0))
            t = tk.Text(win, height=5, wrap="word")
            t.pack(fill="both", expand=True, padx=8)
            t.insert("1.0", sc.get(field, ""))
            boxes[field] = t

        def save():
            for f, t in boxes.items():
                new = t.get("1.0", "end").strip()
                if new != sc.get(f):
                    sc[f] = new
                    # edited content invalidates the generated asset
                    dep = {"narration": "audio", "image_prompt": "image",
                           "video_prompt": "video"}[f]
                    sc["status"][dep] = "—"
            self.project.save()
            self._refresh_table()
            win.destroy()

        ttk.Button(win, text="Save", command=save).pack(pady=8)


if __name__ == "__main__":
    App().mainloop()
