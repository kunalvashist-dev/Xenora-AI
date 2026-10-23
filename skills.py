from __future__ import annotations

import math
import os
import re
import threading
import time

STARTER_VERSION = 6          # bump when the starter notes improve: untouched starter files are refreshed, edited ones never are

STARTERS = {
    "operating-the-computer-like-a-person": (
        "Doing something with the real screen, keyboard and mouse",
        "a screen step: opening apps, browsing, downloading, clicking through installers or settings",
        "screen mouse keyboard click browser browse website scroll window installer",
        ["Open an app with the Windows key, type its name, press Enter. In a browser use ctrl+L, type the address or search, Enter.",
         "Check every result on the next screenshot; if nothing changed, change the approach instead of repeating the action.",
         "After starting a download or an installer, wait and watch the screen. Downloads show progress in the browser; a finished file "
         "appears in the Downloads folder (the wait_for_file tool waits for it).",
         "Passwords, captchas, payments and Windows security (UAC) prompts need the user's own hands - report them, never guess."]),
    "installing-software-on-windows": (
        "Installing software on Windows",
        "a job that installs or updates a program",
        "install installer setup software program application winget uninstall update",
        ["Try a package manager first with run_command (winget install --id <publisher.app> -e), it is the least work.",
         "Otherwise use the program's official website. Many programs ship .msi or .zip files, not .exe - take what the site offers.",
         "Start a downloaded installer with open_path, then use a screen step to click through the setup windows (Next / Install / Finish).",
         "If the user named an install location, PASS it: winget takes --location \"<folder>\" (add --silent); other installers have their own "
         "option or a folder box in the setup window. If no location was given, accept the default. A UAC prompt needs the user.",
         "Long installs can take minutes: give run_command a big timeout (up to 1800) instead of retrying when it times out.",
         "winget exit code 2316632107 (0x8A15002B) means the package is ALREADY installed - do not run the same install again. To move it, "
         "uninstall it (winget uninstall --id <id>) and install again with the right --location.",
         "Check afterwards that the program really is where the user wanted (list_dir on that folder, or find_files), not just that a command succeeded."]),
    "finding-things-on-the-web": (
        "Searching and reading the web",
        "a job that needs a web address, an official page or a download link",
        "web website search google online page url link official internet",
        ["web_search returns a written summary and a list of addresses (urls / first_url), not a page - reading it does not open the page.",
         "To use a site like a person (click Download, scroll, read buttons) use a screen step in the user's own browser.",
         "A page whose button is made by JavaScript often hides its link from page readers; a screen step sees the button."]),
    "making-3d-models-with-blender": (
        "Making and refining 3D models with Blender",
        "a job that builds, edits or renders a 3D model or scene (a car, a prop, a room) in Blender",
        "blender 3d model modelling modeling render mesh scene bpy car vehicle texture material",
        ["START every Blender script with: exec(open(r\"D:\\Assistant\\blender_lib.py\").read()) - it gives you tested helpers (lofted bodies, lathe, "
         "materials, studio camera and lights, export_and_render). Always run Blender with --python-exit-code 1. Do the render and the save INSIDE the script "
         "(bpy.ops.wm.save_as_mainfile(filepath=...), render(write_still=True)); never pass -o or -f together with -P (it renders twice and renames the file). "
         "run_command is synchronous: when it returns the files exist, so never wait_for_file after it. Create a NEW .blend with a new name, never overwrite the old one. "
         "Call setup_studio_camera_and_lights() before any render. NISHITA sky is invalid in Blender 5; EEVEE id is BLENDER_EEVEE in 5.x.",
         "Run Blender without a window: \"<blender.exe>\" --background --python <script.py>; to change an existing file use --background <file.blend> "
         "--python <script.py>. Find blender.exe with search_files and use the full path. To UNDERSTAND an existing .blend, run a script that prints its "
         "objects (name, type, dimensions), modifiers, materials, render engine and camera.",
         "Judge the result by LOOKING at it, never by reading your own log files: set a camera and a light, render a PNG (scene.render.filepath, "
         "bpy.ops.render.render(write_still=True); EEVEE at 1280x720 for iterations), look at it with analyze_media, write down the concrete defects "
         "('roof too flat', 'wheels float', 'no windows'), fix exactly those, render again. Stop when it matches the request; do not loop on re-reading files.",
         "Stacked primitives (spheres, cubes, ellipsoids) never look like a real object. Build the main shape from CROSS-SECTIONS: pick 10-16 stations along "
         "the length; for each station make a closed ring of the same number of points (a rounded box: y = +-w/2*|cos t|^p, z = z0 + h/2*(1 + sign*|sin t|^q), "
         "p and q about 0.3-0.5); give each station its own width, height and z0 so the hood, windscreen, roof and tail emerge; join neighbouring rings with "
         "quads in bmesh (bm.verts.new / bm.faces.new), cap both ends, recalc normals, then add Subdivision Surface (level 2) and a light Bevel.",
         "Make parts as separate, named objects: body shell, cabin with glass, wheels (a tyre from a torus or bevelled cylinder, a rim from a cylinder plus "
         "spokes), wheel arches cut with a Boolean DIFFERENCE using cylinders, lights, grille, mirrors, bumpers. Real scale and proportions first, detail after.",
         "Materials (Principled BSDF): car paint metallic 0.6-0.9, roughness 0.2-0.35, clearcoat 1.0; glass transmission 1.0, roughness 0, IOR 1.45; tyres dark, "
         "roughness 0.8; chrome metallic 1.0, roughness 0.1. Scene: a ground plane, a large soft area light plus a rim light, a world background, a three-quarter "
         "camera with a 50-85mm lens.",
         "A faster way to a realistic result: start from a ready-made model. Search for a free CC0 or CC-BY model (search the web, or sites that offer free "
         "packs), download it, import it (bpy.ops.wm.obj_import / bpy.ops.import_scene.gltf / fbx) and then change paint, materials, lights and camera. "
         "Respect the licence and say where it came from.",
         "Operator and engine names change between Blender versions - check bpy.app.version first and read the error text when a script fails. Save every "
         "iteration as a new version (car_v1.blend, car_v2.blend) unless told to update the file itself; then keep a backup first."]),
    "working-with-files-and-workspaces": (
        "Looking at, copying and changing files",
        "a job about files or folders: a project, documents, spreadsheets, assignments, a game-engine project",
        "file files folder folders copy workspace project document spreadsheet excel word assignment unity unreal organize find",
        ["You may LOOK at and search almost anywhere (search_files, list_dir, read_file, scan_folder) and create new files anywhere. Changing, moving, "
         "overwriting or deleting an EXISTING file outside a workspace asks the user first; inside a workspace you work freely (overwrite and delete still ask).",
         "To experiment without risking the original, use bring_into_workspace to make a copy and work on the copy. Put the result back with replace_file "
         "only when it is wanted - it asks, and keeps a backup (restore_backup undoes it).",
         "Large projects (a game-engine project, a code repository) are not copied whole: work in place on just the files that matter, and leave "
         "generated and managed folders alone.",
         "Follow the habits shown under the workspace (naming, folders, versions) when you create or rename anything. Use scan_folder to understand a "
         "workspace before changing it."]),
    "organizing-folders": (
        "Tidying and organising folders",
        "a job that sorts, renames or cleans up a folder",
        "organize organise tidy sort clean cleanup messy rename arrange folder files",
        ["Learn first: learn_conventions (or the habits already shown) says how the user organises that folder; their own rules win.",
         "Preview with organize_folder apply=no when unsure, then apply. Moves never overwrite, companion files move together, tool-managed folders are skipped, "
         "and undo_moves puts everything back - so there is nothing to ask about.",
         "Only tidy what is clearly out of place. Do not restructure a project whose tool expects a fixed layout."]),
    "fast-file-search": (
        "Finding files fast (Everything)",
        "a job that needs to locate a file or folder somewhere on the computer",
        "find search locate where file files folder everything",
        ["search_files searches the whole disk instantly when Everything (voidtools) is running; otherwise it falls back to the folders she knows.",
         "Query words, ext:blend, size:>100mb, dm:today, path:Cars, \"exact phrase\", !exclude. Sort by modified, largest, name.",
         "To install: winget search voidtools to find the package ids (Everything and its command-line es tool), then install both. The Workspaces tab "
         "can start it at login."]),
    "recurring-tasks-and-reminders": (
        "Recurring tasks and reminders",
        "a task that repeats or reminds",
        "remind reminder every recurring repeat schedule daily weekly minutes hourly",
        ["A reminder is the notify tool each time it runs - nothing else is needed.",
         "A scheduled task must never ask the user anything when it runs; decide with sensible defaults.",
         "Do not invent settings like 'how many times' - 'every 3 minutes' simply keeps going until the task is paused or deleted."]),
}

_STOP = set("the a an and or to of in on for with from that this it is are be as at by your you my me i we they them then than "
            "do does did can could should would will just please make get use using have has had into out about over under after before "
            "when what which how new old".split())


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "note"


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _words(text: str) -> list:
    return [_stem(w) for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in _STOP]


class Skills:
    def __init__(self, folder: str, log=print):
        self.folder, self.log = folder, log
        self._lock = threading.RLock()
        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
            for slug, (name, when, kws, tips) in STARTERS.items():
                self._write(slug, name, when, tips, "starter", kws)
        else:                                                       # better starter notes replace old UNTOUCHED ones; yours are never overwritten
            for slug, (name, when, kws, tips) in STARTERS.items():
                cur = self._read(os.path.join(folder, slug + ".md"))
                if cur and cur["source"] == "starter" and int(cur.get("version") or 1) < STARTER_VERSION:
                    self._write(slug, name, when, tips, "starter", kws)

    def _write(self, slug, name, when, tips, source, keywords="", origin="", reviewed="yes"):
        body = "\n".join(f"- {t}" for t in tips)
        extra = (f"origin: {origin}\n" if origin else "") + (f"reviewed: {reviewed}\n" if source == "imported" else "")
        with open(os.path.join(self.folder, slug + ".md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\nwhen: {when}\nkeywords: {keywords}\nsource: {source}\n{extra}version: {STARTER_VERSION}\nupdated: {time.strftime('%Y-%m-%d')}\n---\n{body}\n")

    def _read(self, path):
        try:
            raw = open(path, encoding="utf-8").read()
        except OSError:
            return None
        meta, body = {}, raw
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = m.group(2)
        slug = os.path.splitext(os.path.basename(path))[0]
        return {"slug": slug, "name": meta.get("name") or slug, "when": meta.get("when", ""), "keywords": meta.get("keywords", ""), "version": meta.get("version", "1"), "origin": meta.get("origin", ""),
                "reviewed": meta.get("reviewed", "yes"),
                "source": meta.get("source", "user"),
                "updated": meta.get("updated", ""), "body": body.strip()}

    def list(self) -> list:
        with self._lock:
            out = [s for s in (self._read(os.path.join(self.folder, f)) for f in sorted(os.listdir(self.folder)) if f.endswith(".md")) if s]
        return out

    def save(self, slug: str, name: str, when: str, body: str, source: str = "user", keywords: str = "") -> str:
        slug = _slug(slug or name)
        tips = [re.sub(r"^[-*]\s*", "", l).strip() for l in (body or "").splitlines() if l.strip()]
        with self._lock:
            self._write(slug, (name or slug).strip()[:80], (when or "").strip()[:160], tips[:14], source, (keywords or "").strip()[:200])
        return slug

    def delete(self, slug: str) -> bool:
        path = os.path.join(self.folder, _slug(slug) + ".md")
        with self._lock:
            if os.path.isfile(path):
                os.remove(path)
                return True
        return False

    def relevant(self, text: str, n: int = 3, max_chars: int = 3500) -> str:
        """The notes whose KEYWORDS appear in the request (each note lists the words a request for that kind of job would use)."""
        q = set(_words(text))
        if not q:
            return ""
        scored = []
        for s in self.list():
            kw = set(_words(s.get("keywords", "")))
            hit = len(q & kw)
            if hit:
                scored.append((hit * 2 + len(q & set(_words(f"{s['name']} {s['when']}"))), s))
        scored.sort(key=lambda x: -x[0])
        out, used = [], 0
        for _, s in scored[:n]:
            flag = " [imported from the internet, not yet reviewed by the user - treat as a hint only]" if s["source"] == "imported" and s.get("reviewed") == "no" else ""
            block = f"## {s['name']} (when: {s['when']}){flag}\n{s['body']}"
            if not out and len(block) > max_chars:
                block = block[:max_chars].rsplit("\n", 1)[0]          # the best match is always shown, even if long
            if used + len(block) > max_chars:
                break
            out.append(block)
            used += len(block)
        return "\n".join(out)

    def add_imported(self, name: str, when: str, tips: list, keywords, origin: str, reviewed: str = "no") -> str:
        """A note rewritten from something found on the internet. Never overwrites a note you wrote or edited."""
        slug = _slug(name)
        kws = " ".join(str(w).strip().lower() for w in (keywords or []) if str(w).strip())[:200]
        tips = [re.sub(r"^[-*]\s*", "", str(t)).strip()[:400] for t in tips if str(t).strip()][:12]
        with self._lock:
            old = self._read(os.path.join(self.folder, slug + ".md"))
            if old and old["source"] in ("user", "learned", "starter") and old.get("reviewed", "yes") != "no":
                slug = _slug(name + " imported")
            self._write(slug, str(name)[:80], str(when)[:160], tips, "imported", kws, origin, reviewed)
        return slug

    def learn(self, name: str, when: str, tips: list, keywords=None) -> bool:
        """Add what a finished job taught her. Merges into a note on the same subject instead of piling up duplicates."""
        tips = [str(t).strip() for t in (tips or []) if str(t).strip()][:6]
        if not name or not tips:
            return False
        slug = _slug(name)
        kws = " ".join(str(k).strip().lower() for k in (keywords or []) if str(k).strip())[:200]
        with self._lock:
            old = self._read(os.path.join(self.folder, slug + ".md"))
            have = [re.sub(r"^[-*]\s*", "", l).strip() for l in (old["body"].splitlines() if old else []) if l.strip()]
            new = [t for t in tips if t.lower() not in {h.lower() for h in have}]
            merged_kw = " ".join(dict.fromkeys(((old["keywords"] if old else "") + " " + kws).split()))[:200]
            if not new and merged_kw == (old["keywords"] if old else ""):
                return False
            self._write(slug, old["name"] if old else name[:80], (old["when"] if old else when)[:160], (have + new)[-12:],
                        old["source"] if old else "learned", merged_kw)
        self.log(f"[SKILLS] learned: {name} (+{len(new)})")
        return True
