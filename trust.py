from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import date

NEVER_LEARN = re.compile(
    r"\b(pay|buy|purchase|checkout|order|donate|delete|remove|erase|transfer|withdraw|uninstall|format|"
    r"send|post|publish|tweet|reply|sign ?out|log ?out|empty trash|overwrite|install|deactivate|"
    r"close account|unsubscribe)\b", re.I)


DESTRUCTIVE = re.compile(
    r"\b(delete|remove|erase|discard|wipe|format|uninstall|overwrite|replace all|empty (the )?(trash|recycle bin)|"
    r"close account|deactivate|shut ?down|restart|power off|buy|purchase|pay|place order|order now|checkout|"
    r"donate|transfer|withdraw)\b", re.I)


def norm(s: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()[:n]


def value_sig(v: str) -> str:
    return hashlib.sha1((v or "").encode("utf-8")).hexdigest()[:6] if v else ""


def make_key(routine: str, domain: str, op: str, label: str, value: str = "") -> str:
    return "|".join([routine, (domain or "").lower(), op, norm(label), value_sig(value)])


class TrustLedger:
    def __init__(self, path: str, *, approvals_needed: int = 3, expire_days: int = 45, today=None, now=None):
        self.path = path
        self.approvals_needed = approvals_needed
        self.expire_days = expire_days
        self._today = today or (lambda: date.today().isoformat())
        self._now = now or time.time
        self._lock = threading.RLock()
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _new() -> dict:
        return {"state": "ask", "approved_days": [], "denied": 0, "granted_by": None,
                "last_used": 0.0, "needs_rearm": False, "note": ""}

    def decision(self, key: str, label: str = "") -> str:
        """'auto' = just do it. 'ask' = get a yes first."""
        if NEVER_LEARN.search(label or ""):
            return "ask"
        with self._lock:
            e = self.data.get(key)
            if not e or e["state"] != "auto":
                return "ask"
            if (self._now() - e["last_used"]) > self.expire_days * 86400:
                e["state"], e["needs_rearm"] = "ask", True
                self._save()
                return "ask"
            return "auto"

    def used(self, key: str):
        with self._lock:
            if key in self.data:
                self.data[key]["last_used"] = self._now()
                self._save()

    def record(self, key: str, approved: bool, label: str = "") -> bool:
        """Record the user's answer. Returns True if this answer just PROMOTED the key to auto."""
        with self._lock:
            e = self.data.setdefault(key, self._new())
            if not approved:
                e.update(state="ask", approved_days=[], denied=e["denied"] + 1, granted_by=None, needs_rearm=False)
                self._save()
                return False
            day = self._today()
            if day not in e["approved_days"]:
                e["approved_days"].append(day)
            e["last_used"] = self._now()
            promoted = False
            if e["state"] != "auto" and not NEVER_LEARN.search(label or ""):
                if e["needs_rearm"] or len(e["approved_days"]) >= self.approvals_needed:
                    e.update(state="auto", granted_by="learned", needs_rearm=False)
                    promoted = True
            self._save()
            return promoted

    def grant(self, key: str, label: str = "") -> tuple[bool, str]:
        """User said 'yes, and stop asking'."""
        if NEVER_LEARN.search(label or ""):
            return False, "I'll always check with you first for that kind of action - it's not something I'll make automatic."
        with self._lock:
            e = self.data.setdefault(key, self._new())
            if self._today() not in e["approved_days"]:
                e["approved_days"].append(self._today())
            e.update(state="auto", granted_by="user", last_used=self._now(), needs_rearm=False)
            self._save()
        return True, ""

    def demote(self, key: str, reason: str):
        """Something unexpected happened: ask again (one fresh approval re-arms; learning isn't wiped)."""
        with self._lock:
            e = self.data.get(key)
            if e and e["state"] == "auto":
                e.update(state="ask", needs_rearm=True, note=reason[:120])
                self._save()

    def revoke(self, routine: str | None = None, key: str | None = None) -> int:
        """'Ask me again' - back to asking, learning wiped. Returns how many keys were reset."""
        n = 0
        with self._lock:
            for k, e in self.data.items():
                if (key and k == key) or (routine and k.startswith(routine + "|")):
                    e.update(state="ask", approved_days=[], granted_by=None, needs_rearm=False)
                    n += 1
            self._save()
        return n

    def grant_all(self, routine: str) -> tuple[int, int]:
        """'Stop asking about X': auto every learnable key of that routine. Returns (granted, refused)."""
        granted = refused = 0
        with self._lock:
            for k, e in list(self.data.items()):
                if k.startswith(routine + "|"):
                    label = k.split("|")[3] if len(k.split("|")) > 3 else ""
                    ok, _ = self.grant(k, label)
                    granted += ok
                    refused += (not ok)
        return granted, refused

    def summary(self, routine: str | None = None) -> list[dict]:
        out = []
        with self._lock:
            for k, e in self.data.items():
                if routine and not k.startswith(routine + "|"):
                    continue
                parts = k.split("|") + [""] * 5
                out.append({"key": k, "routine": parts[0], "domain": parts[1], "op": parts[2], "label": parts[3],
                            "state": e["state"], "days_approved": len(e["approved_days"]),
                            "granted_by": e["granted_by"]})
        return out


OP_CLASS = {"overwrite": "overwrite", "delete": "delete", "edit": "modify", "append": "modify", "move": "modify", "modify": "modify"}
CLASS_VERB = {"overwrite": "overwriting", "delete": "deleting", "modify": "changing"}


class Allowances:
    """File/folder-specific exceptions to "destructive actions ask first". They exist ONLY because the user said so ("stop asking me before
    overwriting car.blend", or 'yes and stop asking' to the question itself) and each can be withdrawn ("ask me again about car.blend").
    ops: "overwrite" | "delete".  A folder allowance covers everything inside it. A bare file name matches any file of that name."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.items = json.load(f)
        except Exception:
            self.items = []

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(str(p)))

    def add(self, ops, target: str, note: str = "") -> dict:
        ops = sorted({OP_CLASS[o] for o in (ops or ["overwrite"]) if o in OP_CLASS}) or ["overwrite"]
        kind = "path" if os.path.isabs(str(target)) else "name"
        with self._lock:
            for e in self.items:
                if e["kind"] == kind and (self._norm(e["target"]) == self._norm(target) if kind == "path" else e["target"].lower() == str(target).lower()):
                    e["ops"] = sorted(set(e["ops"]) | set(ops))
                    self._save()
                    return e
            e = {"id": hashlib.sha1(f"{target}{time.time()}".encode()).hexdigest()[:8], "ops": ops, "target": str(target), "kind": kind,
                 "granted": date.today().isoformat(), "note": note[:140]}
            self.items.append(e)
            self._save()
            return e

    def remove(self, ident: str = "", target: str = "") -> int:
        with self._lock:
            before = len(self.items)
            keep = []
            for e in self.items:
                hit = (ident and e["id"] == ident) or (target and (
                    (e["kind"] == "path" and os.path.isabs(str(target)) and self._norm(e["target"]) == self._norm(target))
                    or os.path.basename(e["target"]).lower() == os.path.basename(str(target)).lower()))
                if not hit:
                    keep.append(e)
            self.items = keep
            self._save()
            return before - len(keep)

    def allows(self, op: str, target: str) -> bool:
        op = OP_CLASS.get(op, op)
        if not target:
            return False
        with self._lock:
            for e in self.items:
                if op not in e["ops"]:
                    continue
                if e["kind"] == "name":
                    if os.path.basename(str(target)).lower() == e["target"].lower():
                        return True
                else:
                    t, base = self._norm(target), self._norm(e["target"])
                    if t == base or t.startswith(base + os.sep):
                        return True
        return False

    def summary(self) -> list:
        with self._lock:
            return [dict(e) for e in self.items]
