from __future__ import annotations

import mimetypes
import os
import time
import re
import webbrowser
from urllib.parse import urlparse

import xenora_tools as xt

AUDIO = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus")
VIDEO = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")
IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def _safe_err(reg, path):
    try:
        reg.safe_path(path)
        return None
    except (PermissionError, ValueError) as e:
        return f"{type(e).__name__}: {e}"


def register_xenora(reg: xt.ToolRegistry, ns: dict, *, files_dir: str, log=print, folders: dict | None = None) -> list:
    added = []
    files_dir = os.path.abspath(files_dir)
    short = lambda s, n=60: str(s)[:n]

    def add(tool):
        reg.register(tool)
        added.append(tool.name)

    find = ns.get("find_file_or_folder")
    if callable(find):
        add(xt.Tool("find_files", "Find files or folders on this computer by name, using my file search (it knows the usual "
                    "folders and other drives).", lambda name: [str(p) for p in (find(str(name)) or [])][:20],
                    {"name": "file or folder name (or part of it)"}, ["name"]))

    dl, folder = ns.get("download_direct"), ns.get("DOWNLOAD_FOLDER")
    if callable(dl) and folder:
        def download_file(url, name=None):
            u = str(url)
            if not re.match(r"^https?://", u, re.I):
                raise ValueError("only http(s) addresses can be downloaded")
            fname = name or os.path.basename(urlparse(u).path.split("?")[0]) or "xenora_download"
            if "." not in fname:
                fname += ".exe"
            if not dl(u, name):
                return {"ok": False, "error": "the download failed"}
            return {"ok": True, "data": {"path": os.path.join(folder, fname)}}
        add(xt.Tool("download_file", "Download a file from a direct web address into my downloads folder (installers, documents, archives).", download_file,
                    {"url": "direct download link", "name": "file name to save as"}, ["url"], risk="act",
                    precheck=lambda a: None if re.match(r"^https?://", str(a.get("url", "")), re.I) else "only http(s) addresses can be downloaded",
                    approval=lambda a: {"domain": urlparse(str(a.get("url", ""))).hostname or "web", "op": "download",
                                        "label": f"download {short(a.get('name') or os.path.basename(urlparse(str(a.get('url', ''))).path) or 'a file', 40)}",
                                        "value": "", "desc": f"Download {short(a.get('name') or a.get('url'), 70)}?"}))

    gen = ns.get("_generate_image_unified")
    if callable(gen):
        def generate_image(prompt, filename=None):
            (img_bytes, mime), provider = gen(str(prompt))
            ext = ".png" if "png" in str(mime) else ".jpg"
            slug = re.sub(r"[^a-z0-9]+", "_", (filename or str(prompt)).lower()).strip("_")[:40] or "image"
            if filename and "." in filename:
                slug, ext = os.path.splitext(re.sub(r"[^A-Za-z0-9_.-]+", "_", filename))[0], os.path.splitext(filename)[1] or ext
            out_dir = os.path.join(files_dir, "images")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, slug + ext)
            with open(path, "wb") as f:
                f.write(img_bytes)
            return {"path": path, "provider": provider, "bytes": len(img_bytes)}
        add(xt.Tool("generate_image", "Generate an image from a description and save it as a file.", generate_image,
                    {"prompt": "what the image should show", "filename": "optional file name"}, ["prompt"], risk="act",
                    approval=lambda a: {"domain": "images", "op": "generate", "label": "generate an image", "value": "",
                                        "desc": f"Generate an image: {short(a.get('prompt'), 70)}?"}))

    cap, ana = ns.get("capture_screenshot"), ns.get("analyze_screen_b64")
    if callable(cap) and callable(ana):
        def look_at_screen(question):
            img, b64, _ = cap()
            if not b64:
                return {"ok": False, "error": "I couldn't capture the screen"}
            title = ns["get_active_window"]() if callable(ns.get("get_active_window")) else ""
            ans = ana(title, b64, str(question))
            if not ans:
                return {"ok": False, "error": "the vision model gave no answer"}
            return {"ok": True, "data": ans}
        add(xt.Tool("look_at_screen", "Take a screenshot and answer a question about what is on the screen.", look_at_screen,
                    {"question": "what to find out"}, ["question"], risk="act",
                    approval=lambda a: {"domain": "screen", "op": "look", "label": "look at the screen", "value": "",
                                        "desc": "Take a look at your screen to answer a question?"}))

    an_a, an_v, vis = ns.get("_analyze_audio"), ns.get("_analyze_video"), ns.get("gemini_vision_call")
    if callable(an_a) or callable(an_v) or callable(vis):
        def analyze_media(path, question=""):
            full = reg.safe_path(path, read=True) if hasattr(reg, "safe_path") else os.path.abspath(path)
            ext = os.path.splitext(full)[1].lower()
            if ext in AUDIO and callable(an_a):
                return an_a(full, question)
            if ext in VIDEO and callable(an_v):
                return an_v(full, question)
            if ext in IMAGE and callable(vis):
                from PIL import Image
                ans, _ = vis("Describe or answer about this image.", str(question or "Describe this image."), [Image.open(full)])
                return ans or ""
            raise ValueError(f"I can't analyse a '{ext}' file")
        add(xt.Tool("analyze_media", "Understand an audio, video or image file: transcribe it, or answer a question about it.",
                    analyze_media, {"path": "file path", "question": "what to find out"}, ["path"]))

    yt = ns.get("download_youtube")
    if callable(yt) and folder:
        def download_media(query, video=False):
            want_video = str(video).lower() in ("true", "1", "yes", "video")
            started = time.time()
            if not yt(str(query), "video" if want_video else "audio"):
                return {"ok": False, "error": "I couldn't retrieve that - it may be unavailable or restricted"}
            new = []
            try:
                for n in os.listdir(folder):
                    p_ = os.path.join(folder, n)
                    if os.path.isfile(p_) and os.path.getmtime(p_) >= started - 1:
                        new.append(p_)
                new.sort(key=os.path.getmtime, reverse=True)
            except OSError:
                pass
            data = {"folder": folder, "files": new[:5], "text": f"downloaded '{short(query, 60)}' into {folder}" + (f" as {os.path.basename(new[0])}" if new else "")}
            if new:
                data["path"] = new[0]
            return {"ok": True, "data": data}
        add(xt.Tool("download_media", "Search YouTube for a song or video and download it (audio by default) into my downloads folder.",
                    download_media, {"query": "song or video title (with artist/version)", "video": "true for video, otherwise audio"},
                    ["query"], risk="act",
                    approval=lambda a: {"domain": "media", "op": "download", "label": f"download {short(a.get('query'), 40)}",
                                        "value": "", "desc": f"Download '{short(a.get('query'), 70)}' from YouTube?"}))

    sched = ns.get("start_scheduled_action")
    if callable(sched):
        def set_reminder(message, minutes):
            m = float(minutes)
            if not 0.1 <= m <= 24 * 60:
                raise ValueError("minutes must be between 0.1 and 1440")
            sched({"kind": "schedule", "action": "notify", "message": str(message), "delay_seconds": int(m * 60)})
            return {"ok": True, "data": {"text": f"reminder set for {m:g} minutes from now"}}
        add(xt.Tool("set_reminder", "Remind the user of something after a number of minutes (up to 24 hours).", set_reminder,
                    {"message": "what to remind them of", "minutes": "minutes from now"}, ["message", "minutes"], risk="act",
                    approval=lambda a: {"domain": "reminders", "op": "set", "label": "set a reminder", "value": "",
                                        "desc": f"Set a reminder in {a.get('minutes')} minutes: {short(a.get('message'), 60)}?"}))

    def computer_info():
        import getpass
        import platform
        import string
        info = {"os": platform.platform(), "user": getpass.getuser(), "home": os.path.expanduser("~"),
                "known_folders": dict(folders or {}), "allowed_folders": list(getattr(reg, "roots", []) or []),
                "my_working_folder": files_dir}
        if os.name == "nt":
            info["drives"] = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        return info
    add(xt.Tool("computer_info", "Facts about this computer: the user's name, home folder, where Desktop/Documents/Downloads really are, "
                "which folders I may use, and the drives. Use this instead of guessing or asking the user for a path.", computer_info))

    def wait_for_file(folder="", pattern="*", timeout=900, recent_seconds=120):
        import fnmatch
        base = reg.safe_path(folder) if folder else (ns.get("DOWNLOAD_FOLDER") or (folders or {}).get("Downloads"))
        if not base or not os.path.isdir(base):
            raise FileNotFoundError("I don't know which folder to watch - say which one")
        timeout = max(5, min(float(timeout), 3600))
        since = time.time() - float(recent_seconds)
        partial = (".crdownload", ".part", ".tmp", ".opdownload", ".download", ".partial")
        deadline, last_sig, stable = time.time() + timeout, None, 0
        while time.time() < deadline:
            cand = []
            for n in os.listdir(base):
                p = os.path.join(base, n)
                if os.path.isfile(p) and not n.lower().endswith(partial) and fnmatch.fnmatch(n.lower(), str(pattern).lower()) \
                        and os.path.getmtime(p) >= since:
                    cand.append(p)
            if cand:
                newest = max(cand, key=os.path.getmtime)
                sig = (newest, os.path.getsize(newest))
                stable = stable + 1 if sig == last_sig else 0
                last_sig = sig
                if stable >= 2:
                    return {"path": newest, "size": os.path.getsize(newest)}
            time.sleep(2)
        raise TimeoutError(f"no finished file matching '{pattern}' appeared in {timeout:g}s")
    add(xt.Tool("wait_for_file", "Wait until a download (or any file) has finished arriving in a folder (default: my downloads folder) and return its path. "
                "Ignores half-downloaded files. Use after starting a download.", wait_for_file,
                {"folder": "folder to watch (default Downloads)", "pattern": "file name pattern, e.g. *.msi or blender*", "timeout": "seconds to wait (max 3600)"}, []))

    def open_path(path):
        import subprocess
        import sys
        full = reg.safe_path(path)
        if not os.path.exists(full):
            raise FileNotFoundError(f"'{path}' doesn't exist")
        if os.name == "nt":
            os.startfile(full)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", full])
        else:
            subprocess.Popen(["xdg-open", full])
        return {"ok": True, "data": {"opened": full}}
    add(xt.Tool("open_path", "Open a file or folder on this computer in its normal program (start an installer, show a document, open a folder).",
                open_path, {"path": "full path of the file or folder"}, ["path"], risk="act",
                precheck=lambda a: (lambda: None)() if not a.get("path") else _safe_err(reg, a.get("path")),
                approval=lambda a: {"domain": "files", "op": "open", "label": f"open {short(os.path.basename(str(a.get('path'))), 40)}",
                                    "value": "", "desc": f"Open '{short(os.path.basename(str(a.get('path'))), 60)}'?"}))

    add(xt.Tool("open_in_my_browser", "Open a web address in the user's normal browser (for the user to look at).",
                lambda url: (webbrowser.open(str(url)) or True), {"url": "http(s) address"}, ["url"], risk="act",
                precheck=lambda a: None if re.match(r"^https?://", str(a.get("url", "")), re.I) else "only http(s) addresses",
                approval=lambda a: {"domain": urlparse(str(a.get("url", ""))).hostname or "web", "op": "open", "label": "open a page for you",
                                    "value": "", "desc": f"Open {short(a.get('url'), 70)} in your browser?"}))

    pw, fin = ns.get("POWER_COMMANDS"), ns.get("_finish_power_action")
    if isinstance(pw, dict) and callable(fin):
        acts = sorted(k for k in pw if k in ("shutdown", "restart"))
        if acts:
            def power_action(action):
                if action not in acts:
                    raise ValueError(f"action must be one of {', '.join(acts)}")
                fin(action)
                return True
            add(xt.Tool("power_action", f"Shut down or restart this computer ({', '.join(acts)}). DESTRUCTIVE - unsaved work is lost.",
                        power_action, {"action": " or ".join(acts)}, ["action"], risk="irreversible",
                        precheck=lambda a: None if a.get("action") in acts else f"action must be one of {', '.join(acts)}",
                        approval=lambda a: {"domain": "power", "op": str(a.get("action")), "label": f"{a.get('action')} the computer",
                                            "value": "", "desc": f"{str(a.get('action')).capitalize()} this computer now?", "force_ask": True}))
    log(f"[CAPS] registered from Xenora: {', '.join(added) or 'nothing'}")
    return added
