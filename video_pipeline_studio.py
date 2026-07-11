#!/usr/bin/env python3
"""
Video Pipeline Studio
=====================
Desktop app (Windows / Mac / Linux) that produces narrated videos end to end.

Auth:   Login with Google Cloud (gcloud) — no API key to paste. Pick a project,
        list & test Gemini models from the API, pick & test a voice.
Engine: script + images + speech come from Gemini; VIDEO comes from Dola
        (dola.com) driven in a real Chrome window.

Three tabs:
  1. Setup      — Google Cloud login, project, model list + test, voice + test.
  2. Pipeline   — topic -> scenes -> images -> speech -> Dola videos -> merge.
  3. Dola Video — generate video from a single prompt, or a batch of prompts.

Requirements on the machine:
  * Python 3.9+ with Tkinter (bundled in the Windows installer)
  * Google Cloud SDK  (the `gcloud` command)      -> https://cloud.google.com/sdk
  * ffmpeg + ffprobe on PATH                       -> winget install ffmpeg
  * pip install playwright   (first Dola run auto-downloads the browser)
"""
import json, os, queue, shutil, subprocess, sys, threading, time, traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gclient
import updater
from dola import DolaSession

STYLE_DEFAULT = "cinematic, photorealistic, dramatic lighting, high detail"
SCRIPT_PROMPT = """You are a professional video director and screenwriter.
Write a video script about the topic below, split into exactly {n} scenes.

Topic: {topic}
Visual style: {style}

Return ONLY valid JSON:
{{"title":"...","scenes":[
 {{"narration":"2-4 spoken sentences of voiceover",
   "image_prompt":"detailed key-frame prompt (subject, composition, camera, lighting, mood, style '{style}'); no text in image",
   "video_prompt":"prompt for an AI video tool animating that scene: camera motion, subject movement, atmosphere, pacing"}}
]}}
Scenes must flow as one continuous story; narration must sound natural aloud."""


# ----------------------------------------------------------------- ffmpeg
def run_cmd(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed:\n{p.stderr[-800:]}")
    return p.stdout


def media_duration(path):
    return float(run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).strip())


def build_scene_clip(video, audio, dest):
    adur = media_duration(audio)
    run_cmd(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video), "-i", str(audio),
             "-map", "0:v:0", "-map", "1:a:0", "-t", f"{adur:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-r", "24", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(dest)])


def concat_clips(clips, dest, workdir):
    lst = Path(workdir) / "concat.txt"
    lst.write_text("".join(f"file '{Path(c).as_posix()}'\n" for c in clips))
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)])


def play_wav(path):
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", str(path)])
        else:
            subprocess.Popen(["aplay", str(path)], stderr=subprocess.DEVNULL)
    except Exception:
        pass


STEPS = ("image", "audio", "video")


class Project:
    def __init__(self, root):
        self.root = Path(root)
        self.file = self.root / "project.json"
        self.data = {"topic": "", "title": "", "scenes": []}

    def load(self):
        if self.file.exists():
            self.data = json.loads(self.file.read_text(encoding="utf-8"))

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def scene_paths(self, i):
        s = self.root / "scenes"; s.mkdir(parents=True, exist_ok=True)
        return {"image": s / f"scene_{i+1:02d}.png", "audio": s / f"scene_{i+1:02d}.wav",
                "video": s / f"scene_{i+1:02d}.mp4", "clip": s / f"scene_{i+1:02d}_final.mp4"}


# ----------------------------------------------------------------- UI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Video Pipeline Studio  v{updater.VERSION}")
        self.geometry("1180x820"); self.minsize(1000, 680)
        self.project = None
        self.worker = None
        self.stop_flag = threading.Event()
        self.msg_q = queue.Queue()
        self.dola = None          # shared DolaSession (opened lazily)
        self.gc = None            # gclient.Gemini once project chosen

        self.v = {
            "project": tk.StringVar(),
            "region": tk.StringVar(value="us-central1"),
            "text_model": tk.StringVar(value="gemini-2.5-pro"),
            "image_model": tk.StringVar(value="gemini-2.5-flash-image"),
            "tts_model": tk.StringVar(value="gemini-2.5-pro-preview-tts"),
            "voice": tk.StringVar(value="Kore"),
            "topic": tk.StringVar(),
            "scenes": tk.StringVar(value="6"),
            "style": tk.StringVar(value=STYLE_DEFAULT),
            "out_dir": tk.StringVar(value=str(Path.home() / "VideoPipelineProjects" / "project1")),
            "account": tk.StringVar(value="not logged in"),
            "dola_dir": tk.StringVar(value=str(Path.home() / "VideoPipelineProjects" / "dola")),
            "dola_prompt": tk.StringVar(value="A cinematic drone shot through a neon-lit cyber city at dusk, 8k"),
        }
        self._build()
        self.after(120, self._poll)
        self._bg(self._refresh_account)

    # ---- layout ----------------------------------------------------------
    def _build(self):
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_setup = ttk.Frame(nb); self.tab_pipe = ttk.Frame(nb); self.tab_dola = ttk.Frame(nb)
        nb.add(self.tab_setup, text="1 · Setup / Auth")
        nb.add(self.tab_pipe, text="2 · Pipeline")
        nb.add(self.tab_dola, text="3 · Dola Video")
        self._build_setup(); self._build_pipe(); self._build_dola()

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=8)
        self.log_box = scrolledtext.ScrolledText(self, height=9, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", padx=8, pady=6)

    def _build_setup(self, pad={"padx": 6, "pady": 4}):
        f = self.tab_setup
        a = ttk.LabelFrame(f, text="Google Cloud"); a.pack(fill="x", padx=8, pady=6)
        r = ttk.Frame(a); r.pack(fill="x")
        ttk.Button(r, text="Login with Google Cloud", command=lambda: self._bg(self._login)).pack(side="left", **pad)
        ttk.Button(r, text="Logout", command=lambda: self._bg(self._logout)).pack(side="left", **pad)
        ttk.Label(r, text="Account:").pack(side="left", **pad)
        ttk.Label(r, textvariable=self.v["account"], foreground="#2c7").pack(side="left", **pad)
        ttk.Button(r, text="Check for updates", command=lambda: self._bg(self._check_update)).pack(side="right", **pad)
        r2 = ttk.Frame(a); r2.pack(fill="x")
        ttk.Label(r2, text="Project:").pack(side="left", **pad)
        self.project_cb = ttk.Combobox(r2, textvariable=self.v["project"], width=34)
        self.project_cb.pack(side="left", **pad)
        ttk.Button(r2, text="Load projects", command=lambda: self._bg(self._load_projects)).pack(side="left", **pad)
        ttk.Label(r2, text="Region:").pack(side="left", **pad)
        ttk.Combobox(r2, textvariable=self.v["region"], values=gclient.REGIONS, width=14).pack(side="left", **pad)

        m = ttk.LabelFrame(f, text="Models  (from the API — pick and Test each)"); m.pack(fill="x", padx=8, pady=6)
        ttk.Button(m, text="List models from API", command=lambda: self._bg(self._list_models)).pack(anchor="w", **pad)
        self.model_cbs = {}
        for key, label in (("text_model", "Script model"), ("image_model", "Image model"),
                           ("tts_model", "Speech (TTS) model")):
            row = ttk.Frame(m); row.pack(fill="x")
            ttk.Label(row, text=label + ":", width=18).pack(side="left", **pad)
            cb = ttk.Combobox(row, textvariable=self.v[key], width=34); cb.pack(side="left", **pad)
            self.model_cbs[key] = cb
            ttk.Button(row, text="Test", command=lambda k=key: self._bg(self._test_model, k)).pack(side="left", **pad)

        vf = ttk.LabelFrame(f, text="Voice"); vf.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(vf); row.pack(fill="x")
        ttk.Label(row, text="Voice:", width=18).pack(side="left", **pad)
        self.voice_cb = ttk.Combobox(row, textvariable=self.v["voice"], values=gclient.VOICES, width=20)
        self.voice_cb.pack(side="left", **pad)
        ttk.Button(row, text="Test voice ▶", command=lambda: self._bg(self._test_voice)).pack(side="left", **pad)

    def _build_pipe(self, pad={"padx": 6, "pady": 4}):
        f = self.tab_pipe
        top = ttk.Frame(f); top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Project folder:").pack(side="left", **pad)
        ttk.Entry(top, textvariable=self.v["out_dir"], width=48).pack(side="left", fill="x", expand=True, **pad)
        ttk.Button(top, text="Browse…", command=self._browse).pack(side="left", **pad)
        ttk.Button(top, text="Load", command=self._load_project).pack(side="left", **pad)
        r2 = ttk.Frame(f); r2.pack(fill="x", padx=8)
        ttk.Label(r2, text="Topic:").pack(side="left", **pad)
        ttk.Entry(r2, textvariable=self.v["topic"]).pack(side="left", fill="x", expand=True, **pad)
        ttk.Label(r2, text="Scenes:").pack(side="left", **pad)
        ttk.Spinbox(r2, from_=1, to=30, textvariable=self.v["scenes"], width=4).pack(side="left", **pad)
        r3 = ttk.Frame(f); r3.pack(fill="x", padx=8)
        ttk.Label(r3, text="Style:").pack(side="left", **pad)
        ttk.Entry(r3, textvariable=self.v["style"], width=70).pack(side="left", **pad)

        btns = ttk.Frame(f); btns.pack(fill="x", padx=8, pady=4)
        for text, cmd in (("1 Script", self.act_script), ("2 Images", lambda: self.act_step("image")),
                          ("3 Speech", lambda: self.act_step("audio")),
                          ("4 Videos (Dola)", lambda: self.act_step("video")),
                          ("5 Merge", self.act_merge), ("▶ Run all", self.act_full),
                          ("⟳ Retry failed", self.act_retry)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Button(btns, text="■ Stop", command=self.stop_flag.set).pack(side="left", padx=3)

        cols = ("scene", "narration", "image", "audio", "video")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        for c, w in (("scene", 55), ("narration", 540), ("image", 90), ("audio", 90), ("video", 100)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="w" if c == "narration" else "center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.tag_configure("failed", background="#5a2020", foreground="white")
        self.tree.tag_configure("done", background="#1f4d2b", foreground="white")
        menu = tk.Menu(self, tearoff=0)
        for lbl, st in (("Regenerate image", "image"), ("Regenerate speech", "audio"),
                        ("Regenerate video", "video")):
            menu.add_command(label=lbl, command=lambda s=st: self._regen(s))
        self.tree.bind("<Button-3>", lambda e: (self.tree.selection_set(self.tree.identify_row(e.y))
                       if self.tree.identify_row(e.y) else None, menu.tk_popup(e.x_root, e.y_root)))

    def _build_dola(self, pad={"padx": 6, "pady": 4}):
        f = self.tab_dola
        info = ("Dola makes the video by driving dola.com in a Chrome window. "
                "Log in once; the session is remembered between runs.")
        ttk.Label(f, text=info, wraplength=1100, foreground="#89f").pack(anchor="w", padx=10, pady=6)
        r = ttk.Frame(f); r.pack(fill="x", padx=8)
        ttk.Button(r, text="Open Dola & log in", command=lambda: self._bg(self._dola_login)).pack(side="left", **pad)
        ttk.Button(r, text="I'm logged in ✓", command=lambda: self.log("Great — you can generate now.")).pack(side="left", **pad)
        ttk.Label(r, text="Save to:").pack(side="left", **pad)
        ttk.Entry(r, textvariable=self.v["dola_dir"], width=44).pack(side="left", **pad)

        single = ttk.LabelFrame(f, text="Single prompt"); single.pack(fill="x", padx=8, pady=6)
        ttk.Entry(single, textvariable=self.v["dola_prompt"]).pack(fill="x", padx=6, pady=4)
        ttk.Button(single, text="Generate video", command=lambda: self._bg(self._dola_single)).pack(anchor="w", padx=6, pady=4)

        batch = ttk.LabelFrame(f, text="Batch — one prompt per line"); batch.pack(fill="both", expand=True, padx=8, pady=6)
        self.batch_txt = tk.Text(batch, height=8, wrap="word"); self.batch_txt.pack(fill="both", expand=True, padx=6, pady=4)
        self.batch_txt.insert("1.0", "A calm sunrise over misty mountains\nWaves crashing on a rocky shore at golden hour\n")
        ttk.Button(batch, text="Generate all", command=lambda: self._bg(self._dola_batch)).pack(anchor="w", padx=6, pady=4)

        acc_frame = ttk.LabelFrame(f, text="Dola Accounts (Auto-Login & Delete Flow) [Optional]"); acc_frame.pack(fill="both", expand=True, padx=8, pady=6)
        ttk.Label(acc_frame, text="Format: email:password:totp_secret (one per line). If provided, these will be used for automated login and generation.").pack(anchor="w", padx=6, pady=2)
        self.dola_accounts_box = scrolledtext.ScrolledText(acc_frame, height=4, width=80)
        self.dola_accounts_box.pack(fill="both", expand=True, padx=6, pady=4)


    # ---- infra -----------------------------------------------------------
    def log(self, m): self.msg_q.put(("log", m))

    def _poll(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("[%H:%M:%S] ") + str(payload) + "\n")
                    self.log_box.see("end"); self.log_box.configure(state="disabled")
                elif kind == "refresh": self._refresh_table()
                elif kind == "progress":
                    self.progress["maximum"] = max(payload[1], 1); self.progress["value"] = payload[0]
                elif kind == "error": messagebox.showerror("Error", payload)
                elif kind == "login_url": self._open_login_dialog(payload)
                elif kind == "set": self.v[payload[0]].set(payload[1])
                elif kind == "models":
                    for k, cb in self.model_cbs.items():
                        cb["values"] = payload.get(k.split("_")[0], [])
                elif kind == "projects": self.project_cb["values"] = payload
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _bg(self, fn, *args):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A job is already running."); return
        self.stop_flag.clear()

        def wrap():
            try: fn(*args)
            except Exception as e:
                self.log(f"ERROR: {e}"); self.msg_q.put(("error", str(e))); traceback.print_exc()
            finally: self.msg_q.put(("refresh", None))
        self.worker = threading.Thread(target=wrap, daemon=True); self.worker.start()

    def _browse(self):
        d = filedialog.askdirectory()
        if d: self.v["out_dir"].set(d)

    def _client(self):
        proj = self.v["project"].get().strip()
        region = self.v["region"].get().strip() or "us-central1"
        if not proj:
            raise RuntimeError("Pick a Google Cloud project on the Setup tab first.")
        if not self.gc or self.gc.project != proj or self.gc.region != region:
            self.gc = gclient.Gemini(proj, region)
        return self.gc

    # ---- self-update -----------------------------------------------------
    def _check_update(self):
        self.log(f"Current version v{updater.VERSION}. Checking GitHub for updates…")
        tag, exe_url, notes = updater.latest_release()
        if not updater.is_newer(tag):
            return self.log(f"You're up to date (latest is v{tag}).")
        if not exe_url:
            return self.log(f"v{tag} is available but has no .exe asset yet.")
        if not messagebox.askyesno("Update available",
                                    f"Version v{tag} is available (you have v{updater.VERSION}).\n\n"
                                    f"{(notes or '')[:400]}\n\nDownload and install now?"):
            return self.log("Update skipped.")
        import tempfile
        dest = os.path.join(tempfile.gettempdir(), "VideoPipelineStudio_new.exe")
        self.log(f"Downloading v{tag}…")
        updater.download(exe_url, dest, progress=lambda g, t: self.msg_q.put(("progress", (g, t))))
        try:
            self.log("Downloaded. Restarting into the new version…")
            updater.apply_and_restart(dest)
        except RuntimeError as e:
            self.log(str(e) + f"\nThe new exe was saved to: {dest}")

    # ---- setup actions ---------------------------------------------------
    def _refresh_account(self):
        self.msg_q.put(("set", ("account", gclient.account() or "not logged in")))

    def _login(self):
        self.log("Getting a Google sign-in link (copy-paste, no browser launched)…")
        url = gclient.login_start()
        self.msg_q.put(("login_url", url))

    def _open_login_dialog(self, url):
        win = tk.Toplevel(self); win.title("Google Cloud login"); win.geometry("680x300")
        ttk.Label(win, text="1)  Copy this link, open it in ANY browser, sign in, then copy the code Google shows:",
                  wraplength=650).pack(anchor="w", padx=12, pady=(12, 4))
        e = ttk.Entry(win, width=100); e.pack(fill="x", padx=12); e.insert(0, url); e.configure(state="readonly")
        ttk.Button(win, text="Copy link",
                   command=lambda: (self.clipboard_clear(), self.clipboard_append(url),
                                    self.log("Link copied to clipboard."))).pack(anchor="w", padx=12, pady=6)
        ttk.Label(win, text="2)  Paste the authorization code here:").pack(anchor="w", padx=12, pady=(10, 4))
        code = tk.StringVar()
        ent = ttk.Entry(win, textvariable=code, width=70); ent.pack(fill="x", padx=12); ent.focus_set()
        def submit():
            win.destroy(); self._bg(self._login_finish, code.get())
        ent.bind("<Return>", lambda _e: submit())
        ttk.Button(win, text="Submit code", command=submit).pack(anchor="w", padx=12, pady=10)

    def _login_finish(self, code):
        if not code.strip():
            return self.log("No code entered — login cancelled.")
        acct = gclient.login_finish(code)
        self.msg_q.put(("set", ("account", acct or "not logged in")))
        if acct:
            self.log(f"Logged in as {acct}. Loading projects…"); self._load_projects()
        else:
            self.log("Login did not complete — check the code and try again.")

    def _logout(self):
        gclient.logout(); self.msg_q.put(("set", ("account", "not logged in"))); self.log("Logged out.")

    def _load_projects(self):
        projs = gclient.list_projects()
        self.msg_q.put(("projects", projs))
        if projs and not self.v["project"].get():
            self.msg_q.put(("set", ("project", projs[0])))
        self.log(f"Found {len(projs)} project(s).")

    def _list_models(self):
        models = self._client().list_models()
        self.msg_q.put(("models", models))
        self.log(f"Models — text:{len(models['text'])} image:{len(models['image'])} tts:{len(models['tts'])}")

    def _test_model(self, key):
        model = self.v[key].get().strip()
        self.log(f"Testing {model}…")
        ok, msg = self._client().test_model(model)
        self.log(("✓ " if ok else "✗ ") + f"{model}: {msg[:160]}")

    def _test_voice(self):
        tmp = Path(self.v["out_dir"].get()); tmp.mkdir(parents=True, exist_ok=True)
        wav = tmp / "_voice_test.wav"
        self.log(f"Generating voice sample ({self.v['voice'].get()})…")
        self._client().tts_wav(self.v["tts_model"].get(), self.v["voice"].get(),
                               "Hi! This is how this voice sounds for your narration.", wav)
        play_wav(wav); self.log("▶ playing sample")

    # ---- pipeline --------------------------------------------------------
    def _ensure_project(self):
        if self.project is None or str(self.project.root) != self.v["out_dir"].get():
            self.project = Project(self.v["out_dir"].get()); self.project.load()
        return self.project

    def _load_project(self):
        self.project = None; p = self._ensure_project()
        self.v["topic"].set(p.data.get("topic", self.v["topic"].get()))
        self._refresh_table(); self.log(f"Loaded {p.root} ({len(p.data['scenes'])} scenes)")

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        if not self.project: return
        for i, sc in enumerate(self.project.data["scenes"]):
            st = sc.setdefault("status", {})
            statuses = [st.get(k) for k in STEPS]
            tag = ("failed",) if "failed" in statuses else (("done",) if all(s == "done" for s in statuses) else ())
            self.tree.insert("", "end", iid=str(i),
                             values=(i + 1, sc["narration"][:90], *(st.get(k, "—") for k in STEPS)), tags=tag)

    def act_script(self):
        if not self.v["topic"].get().strip():
            return messagebox.showwarning("Topic", "Enter a topic first.")
        self._bg(self._job_script)

    def _job_script(self):
        gc = self._client(); p = self._ensure_project()
        topic = self.v["topic"].get().strip()
        self.log(f"Generating script: {topic}")
        data = gc.generate_script(self.v["text_model"].get(),
                                  SCRIPT_PROMPT.format(topic=topic, n=int(self.v["scenes"].get()),
                                                       style=self.v["style"].get()))
        p.data.update(topic=topic, title=data.get("title", topic),
                      scenes=[{**sc, "status": {}} for sc in data["scenes"]])
        p.save(); self.msg_q.put(("refresh", None))
        self.log(f"Script ready: \"{p.data['title']}\" — {len(p.data['scenes'])} scenes")

    def act_step(self, step, only=None, only_failed=False):
        self._bg(self._job_step, step, only, only_failed)

    def _job_step(self, step, only, only_failed):
        gc = self._client(); p = self._ensure_project(); scenes = p.data["scenes"]
        if not scenes: raise RuntimeError("No script yet — run step 1.")
        todo = []
        for i, sc in enumerate(scenes):
            st = sc.setdefault("status", {})
            if only is not None and i not in only: continue
            if only_failed and st.get(step) != "failed": continue
            if only is None and not only_failed and st.get(step) == "done": continue
            todo.append(i)
        if not todo: return self.log(f"[{step}] nothing to do.")
        
        accs = self.dola_accounts_box.get("1.0", "end").strip()
        if step == "video" and accs:
            prompts = [scenes[i]["video_prompt"] for i in todo]
            dests = [p.scene_paths(i)["video"] for i in todo]
            self.log(f"[{step}] starting automated generation for {len(todo)} scenes...")
            self._run_dola_automated(accs, prompts, dests)
            for i in todo:
                sc = scenes[i]
                if p.scene_paths(i)["video"].exists():
                    sc["status"][step] = "done"
                else:
                    sc["status"][step] = "failed"
            p.save()
            self.msg_q.put(("refresh", None))
            return
            
        dola = self._dola_session() if step == "video" else None
        self.msg_q.put(("progress", (0, len(todo))))
        for n, i in enumerate(todo):
            if self.stop_flag.is_set(): return self.log("Stopped.")
            sc = scenes[i]; paths = p.scene_paths(i); sc["status"][step] = "running"
            self.msg_q.put(("refresh", None)); self.log(f"[{step}] scene {i+1}/{len(scenes)}…")
            try:
                if step == "image":
                    gc.generate_image(self.v["image_model"].get(), sc["image_prompt"], paths["image"])
                elif step == "audio":
                    gc.tts_wav(self.v["tts_model"].get(), self.v["voice"].get(), sc["narration"], paths["audio"])
                elif step == "video":
                    dola.generate(sc["video_prompt"], str(paths["video"]))
                sc["status"][step] = "done"; self.log(f"[{step}] scene {i+1} ✓")
            except Exception as e:
                sc["status"][step] = "failed"; sc.setdefault("errors", {})[step] = str(e)
                self.log(f"[{step}] scene {i+1} ✗ {e}")
            p.save(); self.msg_q.put(("refresh", None)); self.msg_q.put(("progress", (n + 1, len(todo))))
        fails = sum(1 for i in todo if scenes[i]["status"].get(step) == "failed")
        self.log(f"[{step}] done: {len(todo)-fails} ok, {fails} failed"
                 + (" — use Retry failed" if fails else ""))

    def act_retry(self): self._bg(self._job_retry)

    def _job_retry(self):
        for step in STEPS:
            if self.stop_flag.is_set(): return
            self._job_step(step, None, True)

    def act_full(self): self._bg(self._job_full)

    def _job_full(self):
        p = self._ensure_project()
        if not p.data["scenes"]: self._job_script()
        for step in STEPS:
            if self.stop_flag.is_set(): return
            self._job_step(step, None, False)
        if not self.stop_flag.is_set(): self._job_merge()

    def act_merge(self): self._bg(self._job_merge)

    def _job_merge(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("ffmpeg/ffprobe not on PATH (Windows: winget install ffmpeg, then reopen).")
        p = self._ensure_project(); scenes = p.data["scenes"]; clips = []
        self.msg_q.put(("progress", (0, len(scenes) + 1)))
        for i, sc in enumerate(scenes):
            if self.stop_flag.is_set(): return
            paths = p.scene_paths(i)
            if not paths["video"].exists() or not paths["audio"].exists():
                raise RuntimeError(f"Scene {i+1} missing video or audio — generate those first.")
            self.log(f"[merge] scene {i+1}: syncing video to narration…")
            build_scene_clip(paths["video"], paths["audio"], paths["clip"]); clips.append(paths["clip"])
            self.msg_q.put(("progress", (i + 1, len(scenes) + 1)))
        final = p.root / "final.mp4"
        self.log("[merge] concatenating…"); concat_clips(clips, final, p.root)
        self.msg_q.put(("progress", (len(scenes) + 1, len(scenes) + 1)))
        self.log(f"DONE ✔  {final}  ({media_duration(final):.1f}s)")

    def _regen(self, step):
        sel = self.tree.selection()
        if sel: self.act_step(step, only={int(sel[0])})

    # ---- Dola ------------------------------------------------------------
    def _dola_session(self):
        if self.dola is None:
            base = Path(self.v["dola_dir"].get() or (Path.home() / "VideoPipelineProjects" / "dola"))
            prof = base / "_chrome_profile"
            self.dola = DolaSession(str(prof), str(base), headless=False, log=self.log)
        return self.dola

    def _dola_login(self):
        self.log("Opening Dola…"); self._dola_session().login()

    def _dola_single(self):
        accs = self.dola_accounts_box.get("1.0", "end").strip()
        if accs:
            out = Path(self.v["dola_dir"].get()); out.mkdir(parents=True, exist_ok=True)
            dest = out / f"dola_{int(time.time())}.mp4"
            self._bg(self._run_dola_automated, accs, [self.v["dola_prompt"].get().strip()], [dest])
        else:
            s = self._dola_session(); out = Path(self.v["dola_dir"].get()); out.mkdir(parents=True, exist_ok=True)
            dest = out / f"dola_{int(time.time())}.mp4"
            s.generate(self.v["dola_prompt"].get().strip(), str(dest))
            self.log(f"Saved {dest}")

    def _dola_batch(self):
        prompts = [l.strip() for l in self.batch_txt.get("1.0", "end").splitlines() if l.strip()]
        if not prompts: return self.log("No prompts.")
        out = Path(self.v["dola_dir"].get()); out.mkdir(parents=True, exist_ok=True)
        accs = self.dola_accounts_box.get("1.0", "end").strip()
        if accs:
            dests = [out / f"dola_{int(time.time())}_{n+1:02d}.mp4" for n in range(len(prompts))]
            self._bg(self._run_dola_automated, accs, prompts, dests)
            return

        s = self._dola_session()
        self.msg_q.put(("progress", (0, len(prompts))))
        for n, pr in enumerate(prompts):
            if self.stop_flag.is_set(): return self.log("Stopped.")
            try:
                dest = out / f"dola_{int(time.time())}_{n+1:02d}.mp4"
                s.generate(pr, str(dest)); self.log(f"[{n+1}/{len(prompts)}] ✓ {dest.name}")
            except Exception as e:
                self.log(f"[{n+1}/{len(prompts)}] ✗ {e}")
            self.msg_q.put(("progress", (n + 1, len(prompts))))
        self.log("Batch complete.")

    def _run_dola_automated(self, accounts_text, prompts, dest_paths):
        import full_lifecycle_video as flv
        from autologin import automate_google_login
        from cloakbrowser import launch_persistent_context_async
        import asyncio

        accounts = [line.strip() for line in accounts_text.splitlines() if line.strip()]
        if not accounts: return self.log("No valid accounts provided.")

        async def _run():
            self.msg_q.put(("progress", (0, len(prompts))))
            acc_idx = 0
            for i, (prompt, dest) in enumerate(zip(prompts, dest_paths)):
                if self.stop_flag.is_set():
                    self.log("Stopped."); break
                if acc_idx >= len(accounts):
                    self.log("Ran out of accounts!"); break
                
                acc = accounts[acc_idx]
                parts = acc.split(':')
                if len(parts) < 3:
                    self.log(f"Invalid account format: {acc}"); acc_idx += 1; continue
                email, pw, totp = parts[0], parts[1], parts[2]
                
                self.log(f"[{i+1}/{len(prompts)}] Auto-login with {email}...")
                prof_dir = str(Path(self.v["dola_dir"].get()) / f"_prof_{email}")
                ok = await automate_google_login(email, pw, totp, prof_dir, lambda m,t: self.log(m))
                if not ok:
                    self.log(f"Failed to login with {email}"); acc_idx += 1; continue
                
                self.log(f"[{i+1}/{len(prompts)}] Generating video...")
                ctx = await launch_persistent_context_async(prof_dir, headless=False)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    res = await flv.generate_and_download_video(page, prompt)
                    if res == "SUCCESS":
                        # Wait and find the newest mp4
                        import glob
                        mp4s = glob.glob(f"{prof_dir}/downloads/*.mp4")
                        if mp4s:
                            newest = max(mp4s, key=os.path.getctime)
                            shutil.copy(newest, str(dest))
                            self.log(f"[{i+1}/{len(prompts)}] ✓ {Path(dest).name}")
                        else:
                            self.log(f"[{i+1}/{len(prompts)}] ✗ Video generated but not found in downloads.")
                    else:
                        self.log(f"[{i+1}/{len(prompts)}] ✗ Generation failed: {res}")
                except Exception as e:
                    self.log(f"[{i+1}/{len(prompts)}] ✗ Error: {e}")
                finally:
                    # Always delete account
                    self.log(f"Deleting account {email}...")
                    await flv.delete_dola_account(page)
                    await ctx.close()
                
                acc_idx += 1
                self.msg_q.put(("progress", (i + 1, len(prompts))))
            self.log("Automated generation complete.")

        asyncio.run(_run())

    def destroy(self):
        if self.dola:
            try: self.dola.close()
            except Exception: pass
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
