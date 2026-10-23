"""
xenora_workspace.py - folders you hand her to work in.

Tell her "the car.blend file is in D:\\Cars, keep improving it" and that folder becomes a WORKSPACE: she may read, create, change
and organise anything inside it (nothing outside the folders she has been given), she gets a short, always-fresh picture of what is
in it at the start of every step, and she keeps a running log of what she did there so the next job picks up where the last stopped.

A folder is only ever added because YOU named it (in a request, or in the TASKS tab). System folders and whole drives are refused.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".svn", "$recycle.bin", "system volume information"}
_WIN_PATH = re.compile(r'"([A-Za-z]:\\[^"\r\n]+)"|\'([A-Za-z]:\\[^\'\r\n]+)\'|([A-Za-z]:\\[^\s"\'<>|*?\r\n]+)')


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}".replace(".0", "")
        n /= 1024
    return f"{n:.1f}GB"


def paths_in(text: str) -> list:
    """Folders named in a message: quoted or plain Windows paths. A path to a file means the folder it sits in."""
    out = []
    for m in _WIN_PATH.finditer(text or ""):
        raw = (m.group(1) or m.group(2) or m.group(3) or "").strip().rstrip(".,;:!?)")
        cand = raw
        # an unquoted path with spaces ("C:\My Cars\x.blend") stops at the first space: try to extend it with the following words
        tail = (text or "")[m.end():].split() if not (m.group(1) or m.group(2)) else []
        for extra in [""] + [" ".join(tail[:k]) for k in range(1, 5)]:
            test = (raw + (" " + extra if extra else "")).rstrip(".,;:!?)")
            if os.path.exists(test):
                cand = test
        if os.path.exists(cand):
            folder = cand if os.path.isdir(cand) else os.path.dirname(cand)
            if folder and folder not in out:
                out.append(folder)
    return out


class Workspaces:
    def __init__(self, path: str, is_blocked=None, log=print):
        self.path, self.is_blocked, self.log = path, is_blocked, log
        self._lock = threading.RLock()
        self._cache = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {"folders": []}
        self.data.setdefault("global", {"conventions": [], "learned": ""})

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def list(self) -> list:
        with self._lock:
            return [w["path"] for w in self.data["folders"]]

    def add(self, path: str):
        """-> (ok, folder-or-reason)"""
        full = os.path.realpath(os.path.expanduser(str(path).strip().strip('"')))
        if os.path.isfile(full):
            full = os.path.dirname(full)
        if not os.path.isdir(full):
            return False, "that folder doesn't exist"
        if len(full.rstrip("\\/")) <= 3 or full == os.path.realpath(os.path.expanduser("~")):
            return False, "that is too broad - name a specific folder"
        if self.is_blocked and self.is_blocked(full):
            return False, "that location is protected"
        with self._lock:
            if not any(os.path.realpath(w["path"]) == full for w in self.data["folders"]):
                self.data["folders"].append({"path": full, "added": time.strftime("%Y-%m-%d %H:%M"), "log": []})
                self._save()
        return True, full

    # ---- how the user organises things: learned from what is already there, then corrected by what they say ---- #
    def _entry(self, path: str):
        full = os.path.realpath(str(path))
        for w in self.data["folders"]:
            if os.path.realpath(w["path"]) == full:
                return w
        return None

    def profile(self, path: str) -> dict:
        with self._lock:
            w = self._entry(path)
            return dict((w or {}).get("profile") or {})

    def set_profile(self, path: str, conventions: list, leave_alone: list):
        with self._lock:
            w = self._entry(path)
            if w is not None:
                pr = w.setdefault("profile", {})
                pr["conventions"] = [str(c)[:240] for c in conventions][:10]
                pr["leave_alone"] = [str(c)[:120] for c in leave_alone][:20]
                pr["learned"] = time.strftime("%Y-%m-%d")
                self._save()

    def set_user_rules(self, path: str, rules: list):
        """What the user said themselves ('assignments go under Uni/<year>/<subject>'): always wins over what she inferred."""
        with self._lock:
            w = self._entry(path)
            if w is not None:
                w.setdefault("profile", {})["user_rules"] = [str(r)[:240] for r in rules if str(r).strip()][:12]
                self._save()

    def set_global(self, conventions: list):
        with self._lock:
            self.data["global"] = {"conventions": [str(c)[:240] for c in conventions][:10], "learned": time.strftime("%Y-%m-%d")}
            self._save()

    def global_conventions(self) -> list:
        with self._lock:
            return list((self.data.get("global") or {}).get("conventions") or [])

    def remove(self, path: str) -> bool:
        full = os.path.realpath(str(path))
        with self._lock:
            n = len(self.data["folders"])
            self.data["folders"] = [w for w in self.data["folders"] if os.path.realpath(w["path"]) != full]
            self._save()
            return len(self.data["folders"]) != n

    # ---- what is in a folder -------------------------------------------------------------------------------- #
    @staticmethod
    def _walk(root: str, depth: int = 3, cap: int = 3000):
        files, n = [], 0
        base = root.rstrip("\\/").count(os.sep)
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".") and d.lower() not in SKIP_DIRS]
            if dp.count(os.sep) - base >= depth:
                dns[:] = []
            for fn in fns:
                if fn.startswith("."):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((os.path.relpath(p, root), st.st_size, st.st_mtime))
                n += 1
                if n >= cap:
                    return files
        return files

    def summary_of(self, root: str) -> str:
        key = os.path.realpath(root)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 20:
            return hit[1]
        files = self._walk(root)
        if not files:
            text = f"{root}: empty"
        else:
            total = sum(s for _, s, _ in files)
            by_dir, by_ext = {}, {}
            for rel, s, m in files:
                top = rel.split(os.sep)[0] if os.sep in rel else ""
                by_dir[top] = by_dir.get(top, 0) + 1
                ext = os.path.splitext(rel)[1].lower() or "(none)"
                by_ext[ext] = by_ext.get(ext, 0) + 1
            newest = sorted(files, key=lambda x: -x[2])[:6]
            big = sorted(files, key=lambda x: -x[1])[:4]
            parts = [f"{root}: {len(files)} files, {human_size(total)}",
                     "types " + ", ".join(f"{e}×{c}" for e, c in sorted(by_ext.items(), key=lambda x: -x[1])[:6])]
            subs = [f"{d}/ ({c})" for d, c in sorted(by_dir.items(), key=lambda x: -x[1]) if d][:6]
            if subs:
                parts.append("folders " + ", ".join(subs))
            parts.append("largest " + ", ".join(f"{rel} {human_size(s)}" for rel, s, _ in big))
            parts.append("most recently changed " + ", ".join(f"{rel} ({time.strftime('%m-%d %H:%M', time.localtime(m))})" for rel, _, m in newest))
            text = "; ".join(parts)
        self._cache[key] = (time.time(), text)
        return text

    def tree_text(self, root: str, depth: int = 3, limit: int = 200, preview: int = 3) -> str:
        """A fuller picture of one folder: every file with size and date, and the first lines of a few small text files."""
        files = sorted(self._walk(root, depth), key=lambda x: x[0].lower())
        lines = [f"{rel}  {human_size(s)}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(m))}" for rel, s, m in files[:limit]]
        if len(files) > limit:
            lines.append(f"... and {len(files) - limit} more")
        shown = 0
        for rel, s, _ in files:
            if shown >= preview:
                break
            if os.path.splitext(rel)[1].lower() in (".txt", ".md", ".py", ".json", ".csv", ".log", ".ini", ".cfg", ".yml", ".yaml") and s < 20000:
                try:
                    with open(os.path.join(root, rel), "r", encoding="utf-8", errors="replace") as f:
                        lines.append(f"--- {rel} (start) ---\n{f.read(400)}")
                    shown += 1
                except OSError:
                    pass
        return "\n".join(lines) or "(empty)"

    def summary(self, max_chars: int = 1800) -> str:
        """What the planner is shown every step: each workspace's contents plus the last few things she did there."""
        out = []
        with self._lock:
            ws = [dict(w) for w in self.data["folders"]]
        for w in ws:
            if not os.path.isdir(w["path"]):
                continue
            line = self.summary_of(w["path"])
            recent = (w.get("log") or [])[-3:]
            if recent:
                line += "; what I did here lately: " + " | ".join(f"{e['at']} {e['text']}" for e in recent)
            pr = w.get("profile") or {}
            rules = list(pr.get("user_rules") or []) + list(pr.get("conventions") or [])
            if rules:
                line += "; how the user organises it (follow this when you create, name or move files): " + " / ".join(rules[:5])
            if pr.get("leave_alone"):
                line += "; leave the structure of: " + ", ".join(pr["leave_alone"][:6])
            out.append(line)
        g = self.global_conventions()
        if out and g:
            out.append("How the user organises files in general: " + " / ".join(g[:5]))
        return "\n".join(out)[:max_chars]

    def record(self, text_with_paths: str, artifacts: list, entry: str):
        """Note what a job did in every workspace it touched (its request or the files it made mention the folder)."""
        low = (text_with_paths or "").lower()
        with self._lock:
            for w in self.data["folders"]:
                root = w["path"].lower()
                if root in low or any(str(a).lower().startswith(root) for a in artifacts or []):
                    w.setdefault("log", []).append({"at": time.strftime("%m-%d %H:%M"), "text": entry[:220]})
                    w["log"] = w["log"][-20:]
            self._save()

    def describe(self) -> list:
        with self._lock:
            ws = [dict(w) for w in self.data["folders"]]
        return [{"path": w["path"], "added": w.get("added", ""), "exists": os.path.isdir(w["path"]),
                 "summary": self.summary_of(w["path"]) if os.path.isdir(w["path"]) else "(missing)", "log": (w.get("log") or [])[-8:],
                 "conventions": (w.get("profile") or {}).get("conventions", []), "leave_alone": (w.get("profile") or {}).get("leave_alone", []),
                 "user_rules": (w.get("profile") or {}).get("user_rules", []), "learned": (w.get("profile") or {}).get("learned", "")} for w in ws]
