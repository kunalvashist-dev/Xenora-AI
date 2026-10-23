from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime

import agent as xa
import planner as xp

TASK_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TRIGGER_TYPES = ("daily", "weekly", "interval", "once", "window", "manual")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _valid_iso(v) -> bool:
    try:
        datetime.fromisoformat(str(v))
        return True
    except ValueError:
        return False
EDITABLE_ROOTS = ("name", "aliases", "objective", "trigger", "inputs", "values", "plan", "options", "enabled",
                  "lessons", "state", "trust_label")
OPTION_TYPES = {"once_per_day": bool, "batch_human": bool, "profile": str, "recipe_scope": str, "max_actions": int,
                "time_budget_s": (int, float), "resume": bool}
INPUT_TYPES = ("text", "urls", "number", "list", "choice")
URL_RX = re.compile(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s,;]*)?", re.I)

TASK_DOC = """A task is a JSON object:
{"id": "snake_case_id (never changes)", "name": "short title", "aliases": ["words people use for it"],
 "objective": "what it should achieve, in plain words",
 "trigger": {"type": "daily", "time": "HH:MM"}            (24-hour clock)
          | {"type": "weekly", "days": ["mon","thu"], "time": "HH:MM"}     (days: mon tue wed thu fri sat sun)
          | {"type": "interval", "every_minutes": 120}
          | {"type": "once", "at": "YYYY-MM-DDTHH:MM"}                   (a single run at that moment, 24-hour clock)
          | {"type": "window", "title_rx": "regex for a window title that starts it", "cooldown_s": 600}
          | {"type": "manual"},
 "inputs": {"name": {"type": "text|urls|number|list|choice", "desc": "...", "ask": "question if missing"}},
 "values": {"name": <the current value of each input>},    (this is where lists such as websites live)
 "plan": {"mode": "adaptive|learned|template", "steps": [ ... ]},
 "options": {"once_per_day": bool, "batch_human": bool, "profile": "vote|form|generic", "max_actions": int, "time_budget_s": number},
 "enabled": true|false, "lessons": ["things learned about how to do this"], "state": {"skip_date": "YYYY-MM-DD"}}
Operations (a list):
 {"op":"set","path":"trigger.time","value":"15:00"}      set/replace any value
 {"op":"remove","path":"options.once_per_day"}            delete a key
 {"op":"add_item","path":"values.sites","value":"https://..."}        append to a list
 {"op":"remove_item","path":"values.sites","value":"the exact item"}  remove from a list
 {"op":"replace_item","path":"values.sites","old":"...","new":"..."}  swap a list item
Paths use dots and [index]: "plan.steps[0].goal". The id cannot change."""


def parse_urls(text: str) -> list:
    out = []
    for m in URL_RX.findall(text or ""):
        u = m.rstrip(".,)")
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


def validate_task(task, registry=None):
    """-> (clean task with defaults, [problems]). Problems are plain sentences (they go back to the model)."""
    errs = []
    if not isinstance(task, dict):
        return None, ["a task must be an object"]
    t = copy.deepcopy(task)
    if not isinstance(t.get("id"), str) or not TASK_ID.match(t["id"]):
        errs.append("'id' must be lowercase letters/digits/underscores")
    if not isinstance(t.get("name"), str) or not t["name"].strip() or len(t["name"]) > 80:
        errs.append("'name' must be short text")
    t.setdefault("aliases", [])
    if not isinstance(t["aliases"], list) or not all(isinstance(a, str) and 0 < len(a) <= 40 for a in t["aliases"]) or len(t["aliases"]) > 12:
        errs.append("'aliases' must be a list of up to 12 short words")
    if not isinstance(t.get("objective"), str) or not t["objective"].strip() or len(t["objective"]) > 1000:
        errs.append("'objective' must be text")
    tr = t.setdefault("trigger", {"type": "manual"})
    if not isinstance(tr, dict) or tr.get("type") not in TRIGGER_TYPES:
        errs.append("trigger.type must be daily, weekly, interval, once, window or manual")
    elif tr["type"] in ("daily", "weekly") and not (isinstance(tr.get("time"), str) and HHMM.match(tr["time"])):
        errs.append("trigger.time must be a 24-hour time like '15:00'")
    elif tr["type"] == "weekly" and not (isinstance(tr.get("days"), list) and tr["days"]
                                         and all(str(d).lower()[:3] in DAYS for d in tr["days"])):
        errs.append("trigger.days must be a list like ['mon','thu']")
    elif tr["type"] == "interval" and not (isinstance(tr.get("every_minutes"), (int, float)) and not isinstance(tr.get("every_minutes"), bool)
                                           and 1 <= tr["every_minutes"] <= 60 * 24 * 31):
        errs.append("trigger.every_minutes must be a number of minutes (1 or more)")
    elif tr["type"] == "once" and not _valid_iso(tr.get("at")):
        errs.append("trigger.at must look like 2026-10-20T09:30")
    elif tr["type"] == "window":
        try:
            re.compile(str(tr.get("title_rx", "")))
            if not tr.get("title_rx"):
                raise re.error("empty")
        except re.error:
            errs.append("trigger.title_rx must be a valid, non-empty regular expression")
        if not isinstance(tr.get("cooldown_s", 0), (int, float)) or tr.get("cooldown_s", 0) < 0:
            errs.append("trigger.cooldown_s must be a number of seconds")
    ins = t.setdefault("inputs", {})
    if not isinstance(ins, dict):
        errs.append("'inputs' must be an object")
        ins = {}
    for k, spec in ins.items():
        if not isinstance(spec, dict) or spec.get("type", "text") not in INPUT_TYPES:
            errs.append(f"input '{k}' needs a type from {', '.join(INPUT_TYPES)}")
        elif spec.get("pattern"):
            try:
                re.compile(spec["pattern"])
            except re.error:
                errs.append(f"input '{k}' has an invalid pattern")
    vals = t.setdefault("values", {})
    if not isinstance(vals, dict):
        errs.append("'values' must be an object")
        vals = {}
    for k, v in vals.items():
        spec = ins.get(k)
        if spec is None:
            errs.append(f"value '{k}' has no matching input")
        elif spec.get("type") in ("urls", "list") and not isinstance(v, list):
            errs.append(f"values.{k} must be a list")
        elif spec.get("type") == "urls" and not all(isinstance(u, str) and re.match(r"^https?://", u, re.I) for u in v):
            errs.append(f"values.{k} must contain full http(s) addresses")
        elif spec.get("pattern") and spec.get("type") == "text" and isinstance(v, str) and not re.search(spec["pattern"], v):
            errs.append(f"values.{k} doesn't look right")
        elif spec.get("pattern") and spec.get("type") == "list" and not all(re.search(spec["pattern"], str(x)) for x in v):
            errs.append(f"values.{k} has an item that doesn't look right")
    plan = t.setdefault("plan", {"mode": "adaptive", "steps": []})
    if not isinstance(plan, dict) or plan.get("mode", "adaptive") not in ("adaptive", "learned", "template", "llm"):
        errs.append("plan.mode must be adaptive, learned or template")
    elif not isinstance(plan.get("steps", []), list):
        errs.append("plan.steps must be a list")
    elif plan.get("steps") and registry is not None and all(k in vals for k in ins):
        errs += [f"plan: {e}" for e in xp.validate_plan({"steps": xp.expand_foreach(plan["steps"], vals)}, registry, list(vals))]
    plan.setdefault("steps", [])
    plan.setdefault("mode", "adaptive")
    opts = t.setdefault("options", {})
    if not isinstance(opts, dict):
        errs.append("'options' must be an object")
    else:
        for k, v in opts.items():
            if k not in OPTION_TYPES:
                errs.append(f"unknown option '{k}'")
            elif not isinstance(v, OPTION_TYPES[k]) or (isinstance(v, bool) and OPTION_TYPES[k] is int):
                errs.append(f"option '{k}' has the wrong type")
    if not isinstance(t.setdefault("enabled", True), bool):
        errs.append("'enabled' must be true or false")
    t.setdefault("lessons", [])
    if not isinstance(t["lessons"], list) or not all(isinstance(x, str) for x in t["lessons"]) or len(t["lessons"]) > 30:
        errs.append("'lessons' must be a list of short sentences")
    if not isinstance(t.setdefault("state", {}), dict):
        errs.append("'state' must be an object")
    elif t["state"].get("skip_date") not in (None, "") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(t["state"]["skip_date"])):
        errs.append("state.skip_date must look like 2026-10-14")
    return (t if not errs else None), errs


def _tokens(path: str):
    toks = []
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", str(path or "")):
        toks.append(int(part[1:-1]) if part.startswith("[") else part)
    return toks


def _walk(root, toks):
    cur = root
    for t in toks[:-1]:
        try:
            cur = cur[t]
        except (KeyError, IndexError, TypeError):
            raise KeyError(f"'{t}' doesn't exist there")
    return cur, toks[-1]


def _find_item(lst, value):
    low = str(value).strip().lower()
    exact = [i for i, x in enumerate(lst) if str(x).strip().lower() == low]
    if len(exact) == 1:
        return exact[0], None
    part = [i for i, x in enumerate(lst) if low and low in str(x).lower()]
    if len(part) == 1:
        return part[0], None
    return None, ("no item matches '%s' (items: %s)" % (value, "; ".join(map(str, lst))[:200]) if not part
                  else "'%s' matches several items: %s" % (value, "; ".join(str(lst[i]) for i in part)[:200]))


def apply_ops(task: dict, ops, registry=None):
    """-> (new task | None, [problems], [changes]). All-or-nothing: any problem means nothing is applied."""
    if not isinstance(ops, list) or not ops:
        return None, ["no changes were proposed"], []
    new, errs, changes = copy.deepcopy(task), [], []
    for op in ops[:20]:
        if not isinstance(op, dict):
            errs.append("each change must be an object")
            continue
        kind, path = op.get("op"), op.get("path")
        toks = _tokens(path)
        if not toks or toks[0] not in EDITABLE_ROOTS:
            errs.append(f"'{path}' can't be changed")
            continue
        try:
            parent, last = _walk(new, toks)
            if kind == "set":
                old = parent.get(last) if isinstance(parent, dict) else (parent[last] if last < len(parent) else None)
                if isinstance(parent, dict) or isinstance(last, int) and last < len(parent):
                    parent[last] = op.get("value")
                else:
                    raise KeyError("that position doesn't exist")
                changes.append({"op": "set", "path": path, "old": old, "new": op.get("value")})
            elif kind == "remove":
                old = parent[last]
                del parent[last]
                changes.append({"op": "remove", "path": path, "old": old})
            elif kind in ("add_item", "remove_item", "replace_item"):
                if isinstance(parent, dict) and last not in parent and kind == "add_item":
                    parent[last] = []
                lst = parent[last]
                if not isinstance(lst, list):
                    raise TypeError("that isn't a list")
                if kind == "add_item":
                    v = op.get("value")
                    if str(v).strip().lower() not in [str(x).strip().lower() for x in lst]:
                        lst.append(v)
                        changes.append({"op": "add_item", "path": path, "value": v})
                else:
                    idx, why = _find_item(lst, op.get("value" if kind == "remove_item" else "old"))
                    if idx is None:
                        errs.append(f"{path}: {why}")
                        continue
                    old = lst[idx]
                    if kind == "remove_item":
                        lst.pop(idx)
                        changes.append({"op": "remove_item", "path": path, "value": old})
                    else:
                        lst[idx] = op.get("new")
                        changes.append({"op": "replace_item", "path": path, "old": old, "new": op.get("new")})
            else:
                errs.append(f"unknown operation '{kind}'")
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            errs.append(f"{path}: {str(e).strip(chr(39))[:120]}")
    if errs:
        return None, errs, []
    clean, verrs = validate_task(new, registry)
    if verrs:
        return None, verrs, []
    return clean, [], changes


def describe_changes(changes: list) -> list:
    """Plain facts about what changed (the voice layer says it naturally)."""
    out = []
    for c in changes:
        if c["op"] == "set":
            out.append(f"{c['path']}: {json.dumps(c['old'], default=str)[:60]} -> {json.dumps(c['new'], default=str)[:60]}")
        elif c["op"] == "remove":
            out.append(f"{c['path']}: removed")
        elif c["op"] == "add_item":
            out.append(f"{c['path']}: added {str(c['value'])[:80]}")
        elif c["op"] == "remove_item":
            out.append(f"{c['path']}: removed {str(c['value'])[:80]}")
        else:
            out.append(f"{c['path']}: {str(c['old'])[:50]} -> {str(c['new'])[:50]}")
    return out


class TaskStore:
    def __init__(self, path: str, today=None):
        self.path, self._lock = path, threading.RLock()
        self._today = today or (lambda: datetime.now().strftime("%Y-%m-%d"))
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}
        for k in ("tasks", "state", "history"):
            self.data.setdefault(k, {})

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def ids(self):
        return list(self.data["tasks"])

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data["tasks"])

    def get(self, tid):
        with self._lock:
            t = self.data["tasks"].get(tid)
            return copy.deepcopy(t) if t else None

    def put(self, task: dict, record: bool = True):
        with self._lock:
            old = self.data["tasks"].get(task["id"])
            if record and old:
                h = self.data["history"].setdefault(task["id"], [])
                h.append(copy.deepcopy(old))
                del h[:-10]
            self.data["tasks"][task["id"]] = copy.deepcopy(task)
            self._save()

    def delete(self, tid):
        with self._lock:
            self.data["tasks"].pop(tid, None)
            self.data["state"].pop(tid, None)
            self.data["history"].pop(tid, None)
            self._save()

    def undo(self, tid):
        with self._lock:
            h = self.data["history"].get(tid) or []
            if not h:
                return None
            prev = h.pop()
            self.data["tasks"][tid] = prev
            self._save()
            return copy.deepcopy(prev)

    def purge_ephemeral(self, max_age_s: float = 86400):
        """One-off tasks made from a plain request are only kept while they might still be retried. Returns ids removed."""
        gone = []
        with self._lock:
            for tid, t in list(self.data["tasks"].items()):
                if t.get("ephemeral") and time.time() - float(t.get("created_at", 0) or 0) > max_age_s:
                    gone.append(tid)
            for tid in gone:
                self.data["tasks"].pop(tid, None)
                self.data["state"].pop(tid, None)
                self.data["history"].pop(tid, None)
            if gone:
                self._save()
        return gone

    def get_state(self, tid, key, default=None):
        with self._lock:
            return copy.deepcopy(self._st(tid).get(key, default))

    def set_state(self, tid, key, value):
        with self._lock:
            self._st(tid)[key] = copy.deepcopy(value)
            self._save()

    def _st(self, tid):
        return self.data["state"].setdefault(tid, {})

    def is_done(self, tid, key):
        with self._lock:
            d = self._st(tid).get("done") or {}
            return d.get("date") == self._today() and key in d.get("keys", [])

    def mark_done(self, tid, key):
        with self._lock:
            s = self._st(tid)
            d = s.get("done") or {}
            if d.get("date") != self._today():
                d = {"date": self._today(), "keys": []}
            if key not in d["keys"]:
                d["keys"].append(key)
            s["done"] = d
            self._save()

    def save_plan(self, tid, steps, criteria):
        with self._lock:
            t = self.data["tasks"].get(tid)
            if t:
                t["plan"] = {"mode": "learned", "steps": copy.deepcopy(steps), "criteria": list(criteria or [])}
                self._save()

    def save_mission(self, tid, m):
        with self._lock:
            self._st(tid)["mission"] = copy.deepcopy(m)
            self._save()

    def load_mission(self, tid):
        with self._lock:
            return copy.deepcopy(self._st(tid).get("mission"))

    def clear_mission(self, tid):
        with self._lock:
            self._st(tid).pop("mission", None)
            self._save()

    def add_lessons(self, tid, lessons):
        with self._lock:
            t = self.data["tasks"].get(tid)
            if not t:
                return
            cur = t.setdefault("lessons", [])
            for l in lessons:
                if str(l).strip() and str(l).lower() not in [c.lower() for c in cur]:
                    cur.append(str(l).strip())
            del cur[:-20]
            self._save()


class Editor:
    def __init__(self, planner: xp.Planner, registry=None, log=print):
        self.planner, self.registry, self.log = planner, registry, log

    def propose(self, task: dict, instruction: str, context: str = "") -> dict:
        """-> {"ops","new","changes"} | {"clarify": str} | {"not_an_edit": True} | {"error": str}"""
        if not self.planner.available:
            return {"error": "no language model is connected"}
        view = copy.deepcopy(task)
        view["plan"] = {**view.get("plan", {}), "steps": view.get("plan", {}).get("steps", [])[:12]}
        sysm = ("You change an automated task for the user, exactly as they ask. " + TASK_DOC +
                '\nReply with ONE JSON object: {"ops":[...]} to change it, or {"clarify":"one short question"} if it is '
                'genuinely ambiguous, or {"not_an_edit":true} if the user is not asking to change this task. '
                "Convert times to 24-hour HH:MM. Items you remove must be copied exactly from the task. Make only the "
                "changes asked for. Use the recent events to resolve words like 'this website' or 'that one'.")
        user = (f"Now: {datetime.now():%A %Y-%m-%d %H:%M}\nTask:\n{json.dumps(view, ensure_ascii=False)[:3500]}\n"
                f"Recent events:\n{context or '(none)'}\nThe user said: {instruction}")
        err = ""
        for tier in (0, 0, 1):
            prompt = user if not err else f"{user}\n\nYour previous reply was rejected: {err}\nReply again with ONLY the corrected JSON."
            try:
                raw = self.planner._call(sysm, prompt, tier)
            except Exception as e:  # noqa: BLE001
                err = f"model error: {str(e)[:80]}"
                continue
            obj = None
            for key in ("ops", "clarify", "not_an_edit"):
                obj = xa.extract_json(raw, key=key)
                if obj is not None:
                    break
            if obj is None:
                err = f"no JSON object in: {str(raw)[:80]!r}"
                continue
            if obj.get("not_an_edit"):
                return {"not_an_edit": True}
            if obj.get("clarify"):
                return {"clarify": str(obj["clarify"])[:200]}
            new, problems, changes = apply_ops(task, obj.get("ops"), self.registry)
            if not problems:
                return {"ops": obj["ops"], "new": new, "changes": changes}
            err = "; ".join(problems[:5])
        return {"error": err}


class AutomationContext:
    """What her automations said and did lately - so a reply to them is understood as a reply to them."""

    def __init__(self, now=None, maxlen: int = 40):
        self._now, self.maxlen, self.events, self.running = now or time.time, maxlen, [], {}
        self._lock = threading.Lock()

    def add(self, kind: str, text: str, task: str = None, **meta):
        with self._lock:
            self.events.append({"t": self._now(), "kind": kind, "text": str(text)[:300], "task": task, **meta})
            del self.events[:-self.maxlen]

    def fresh(self, window_s: float = 900) -> bool:
        with self._lock:
            return bool(self.running) or any(e["kind"] == "automated" and self._now() - e["t"] <= window_s for e in self.events)

    def last_task(self):
        with self._lock:
            for e in reversed(self.events):
                if e.get("task"):
                    return e["task"]
        return None

    def text(self, n: int = 10, ages: bool = False) -> str:
        with self._lock:
            now = self._now()
            def age(e):
                m = int((now - e["t"]) // 60)
                return f" {m}m ago" if ages else ""
            return "\n".join(f"[{e['kind']}{' ' + e['task'] if e.get('task') else ''}{age(e)}] {e['text']}" for e in self.events[-n:])


DEFAULT_CUES = ("every", "daily", "weekly", "schedule", "automate", "automation", "remind", "whenever", "each time", "task",
                "routine", "plan", "retry", "try again", "again", "stop", "pause", "resume", "cancel", "change", "remove",
                "add", "edit", "update", "undo", "skip", "continue", "log in", "login", "sign in", "done", "later")
INTENTS = ("run_task", "retry", "edit_task", "create_task", "delete_task", "trust", "explain", "undo", "list_tasks",
           "self_check", "mission_note", "adhoc", "stop_run", "allow_action", "action", "chat")
NEEDS_LIVE_CONTEXT = ("mission_note", "stop_run")      # only meaningful while something is running / waiting / just spoke


class Router:
    """The front door for every message. The MODEL decides whether a message is about her automations, a new
    multi-step job, a simple built-in action, or just conversation. Chat is the default: if the model is unsure, can't
    be reached, or its answer contradicts what is actually going on (e.g. "stop it" when nothing is running), the
    message is NOT hijacked - it goes on to ordinary conversation. The word list is only a fallback when the model
    can't be reached, and it never decides anything otherwise."""

    def __init__(self, store: TaskStore, planner: xp.Planner, context: AutomationContext, cues=DEFAULT_CUES,
                 always_route: bool = True, waiting=None, recent=None, log=print):
        self.store, self.planner, self.ctx, self.cues, self.always, self.log = store, planner, context, tuple(cues), always_route, log
        self.waiting = waiting or (lambda: False)          # () -> is a question of mine waiting for the user's answer?
        self.recent = recent or (lambda: "")             # () -> files she recently made/changed (what 'that report' refers to)
        self.degraded = False                              # set when the model couldn't be reached on the last attempt

    def _mentions(self, low: str) -> bool:
        for t in self.store.all().values():
            names = [t["name"].lower(), t["id"].replace("_", " ")] + [a.lower() for a in t.get("aliases", [])]
            vals = [str(x).lower() for v in (t.get("values") or {}).values() for x in (v if isinstance(v, list) else [v])]
            hosts = [xa.host(v) for v in vals if v.startswith("http")]
            if any(n and n in low for n in names + hosts + [v for v in vals if 4 <= len(v) <= 40 and not v.startswith("http")]):
                return True
        return False

    def gate(self, text: str) -> bool:
        low = (text or "").lower()
        if (self.always and not self.degraded) or self.ctx.fresh():
            return True                                    # even with no tasks yet: a new job may need planning
        return self._mentions(low) or any(c in low for c in self.cues)   # fallback only (model unavailable)

    def route(self, text: str):
        """-> {"intent","task","mode","clarify"} or None (= not mine: ordinary chat / the built-in action handlers)."""
        if not self.planner.available:
            return None
        tasks = self.store.all()
        running, waiting, fresh = list(self.ctx.running), self.waiting(), self.ctx.fresh(900)
        lines = [f"{t['id']}: {t['name']} | aliases: {', '.join(t.get('aliases', []))} | trigger: {json.dumps(t.get('trigger'))} | "
                 f"{'one-off job' if t.get('ephemeral') else ('on' if t.get('enabled', True) else 'paused')}" for t in tasks.values()]
        sysm = ("You route the user's message for a personal assistant. Reply with ONE JSON object: "
                '{"intent":"...","task":"<task id or null>","mode":null,"path":null,"ops":null,"clarify":null,"confidence":"high|low"}.\nIntents:\n'
                " chat - Greetings, small talk, opinions, questions, anything conversational. A greeting is always chat. A request to DO something on "
                "the computer is NEVER chat.\n"
                " action - ONE simple immediate request for a built-in ability: open an app or website, play or download media, "
                "a web search or quick lookup, a question about what is on the screen, one file operation, read or write one file, "
                "make one image, a reminder, watching a window, shutting down. These are handled elsewhere.\n"
                " adhoc - the user wants a JOB done that needs several steps, tools, files, websites, or adapting along the way "
                "(research then write it up, operate a website, install or set up software, deploy something, organise files, chain several apps). "
                "It is a one-off, to start now. If they say it should repeat or run at a time, use create_task instead. ALSO adhoc: "
                "any request about something I made or did earlier (that file, the report, what you just did) - the built-in "
                "action handlers have no memory of it.\n"
                " create_task - the user describes a NEW automation: something that should run on a schedule, at a time, "
                "repeatedly, or when something happens.\n"
                " run_task - run an existing task now.\n retry - try an existing task again after it didn't finish.\n"
                " edit_task - change anything about an existing task: schedule/time, websites or other inputs, steps, name, "
                "pause/resume it, skip a day...\n delete_task - remove a task entirely\n"
                " trust - how often I ask permission for a task (mode: stop_asking | ask_again)\n"
                " explain - the user asks about my tasks: what they are, what happened, status, why something failed\n"
                " undo - undo the last change to a task\n list_tasks - what tasks do I have\n self_check - check my own health\n"
                " mission_note - the user tells me something about a task that is running or waiting (e.g. 'I'll log in myself')\n"
                " stop_run - the user wants a task that is running right now to stop\n"
                " allow_action - the user says to stop (or start again) asking before a destructive action on a SPECIFIC file or folder, e.g. "
                "'stop asking me before overwriting car.blend' or 'ask me again about car.blend'. mode: allow | ask_again; path: the file or "
                "folder as they named it; ops: a list of overwrite, delete and/or modify (overwrite if they didn't say).\n"
                "Rules: when unsure between chat and a task intent for something conversational, choose chat; when the message asks for something to be done, choose adhoc (or action for a single simple thing). Only use a task intent when the message is clearly about a task listed below, "
                "or is a direct reaction to one of MY RECENT automated messages shown below (events show how long ago they were; "
                "old ones are stale). mission_note and stop_run are only possible when something is running or I am waiting for "
                "them. Set confidence low if you are guessing. If the task is unclear and several exist, put a short question "
                "in clarify.")
        user = (f"Tasks:\n{chr(10).join(lines) or '(none)'}\nRecent automation events (oldest first):\n{self.ctx.text(8, ages=True) or '(none)'}\n"
                f"Running now: {', '.join(running) or 'nothing'}. Waiting for the user's answer: {'yes' if waiting else 'no'}.\n"
                f"Files I recently made or changed: {self.recent() or 'none'}\n"
                f"Now: {datetime.now():%A %Y-%m-%d %H:%M}\nUser message: {text}")
        obj, reached = None, False
        for tier in (1, 0):                                # the small fast model first; the better one only if needed
            try:
                obj = xa.extract_json(self.planner._call(sysm, user, tier), key="intent")
                reached = True
            except Exception as e:  # noqa: BLE001
                self.log(f"[ROUTER] model error: {e}")
                obj = None
            if obj and obj.get("intent") in INTENTS:
                break
            obj = None
        self.degraded = not reached
        if not obj or obj["intent"] in ("chat", "action"):
            return None
        intent = obj["intent"]
        if str(obj.get("confidence", "high")).lower() == "low" and not obj.get("clarify"):
            return None                                    # guessing is not a reason to take over the conversation
        if intent in NEEDS_LIVE_CONTEXT and not (running or waiting or fresh):
            return None                                    # nothing is happening that this could be about
        tid = obj.get("task") if obj.get("task") in tasks else None
        if intent in ("run_task", "retry", "edit_task", "delete_task", "trust", "undo", "mission_note", "explain", "stop_run") and not tid:
            if intent in ("mission_note", "stop_run") and len(running) == 1:
                tid = running[0]
            elif len([t for t in tasks.values() if not t.get("ephemeral")]) == 1:
                tid = next(k for k, t in tasks.items() if not t.get("ephemeral"))
            elif self.ctx.last_task() in tasks:
                tid = self.ctx.last_task()
        return {"intent": intent, "task": tid, "mode": obj.get("mode"), "clarify": obj.get("clarify"),
                "path": obj.get("path"), "ops": obj.get("ops")}


class InputResolver:
    def __init__(self, store, planner, mem_search, mem_add, ask_text, notify=None, log=print):
        self.store, self.planner, self.mem_search, self.mem_add = store, planner, mem_search, mem_add
        self.ask_text, self.notify, self.log = ask_text, notify or (lambda *a, **k: None), log
        self.last_missing: list = []

    def _valid(self, spec, v) -> bool:
        pat = spec.get("pattern")
        if spec.get("type") in ("list", "urls"):
            return bool(v) and isinstance(v, list) and all((not pat) or re.search(pat, str(x)) for x in v)
        return bool(str(v).strip()) and ((not pat) or bool(re.search(pat, str(v))))

    def _from_memory(self, name, spec):
        q = spec.get("memory_query") or spec.get("desc") or name
        try:
            mem = "\n".join(dict.fromkeys(self.mem_search(q) or []))
        except Exception:
            return None
        if not mem.strip():
            return None
        typ = spec.get("type", "text")
        if typ == "urls":
            flt = spec.get("line_filter")
            lines = [l for l in mem.splitlines() if (not flt or re.search(flt, l, re.I))]
            found = []
            for l in lines:
                for u in parse_urls(l):
                    if u not in found:
                        found.append(u)
            return found or None
        if not self.planner.available:
            return None
        key = "values" if typ == "list" else "value"
        obj, _ = self.planner._json(
            "Extract facts from the notes. Reply with ONE JSON object: "
            + ('{"values":["..."]}' if typ == "list" else '{"value":"..."}')
            + f". Extract: {spec.get('desc') or name}. Use null/[] if the notes don't say.", mem, key, tiers=(0,))
        cand = (obj or {}).get(key)
        if typ == "list":
            got = [str(c).strip() for c in (cand or []) if str(c).strip() and str(c).lower() in mem.lower()]
            return got or None                                        # must literally appear in memory: no inventions
        c = str(cand or "").strip()
        return c if c and c.lower() in mem.lower() else None

    def resolve(self, task: dict):
        """-> values dict (also saved on the task) or None if something is still missing."""
        vals = dict(task.get("values") or {})
        changed = False
        self.last_missing = []
        for name, spec in (task.get("inputs") or {}).items():
            if name in vals and self._valid(spec, vals[name]):
                continue
            v = self._from_memory(name, spec)
            if v is None or not self._valid(spec, v):
                ans = self.ask_text(spec.get("ask") or f"What should I use for {spec.get('desc') or name}?")
                if not ans:
                    self.last_missing.append(str(spec.get("desc") or name))
                    return None
                typ = spec.get("type", "text")
                v = parse_urls(ans) if typ == "urls" else ([x for x in re.split(r"[,\s]+|\band\b", ans) if x] if typ == "list" else ans.strip())
                if not self._valid(spec, v):
                    self.notify("That doesn't look right - say 'run it' to try again.", kind="error")
                    return None
                if spec.get("save"):
                    try:
                        self.mem_add(spec["save"].replace("{value}", " ".join(v) if isinstance(v, list) else str(v)), ["automation", task["id"]])
                    except Exception:
                        pass
            vals[name] = v
            changed = True
        if changed:
            cur = self.store.get(task["id"])
            if cur:
                cur["values"] = {**cur.get("values", {}), **vals}
                self.store.put(cur, record=False)
        return vals


class TaskFactory:
    def __init__(self, planner: xp.Planner, store: TaskStore, registry=None):
        self.planner, self.store, self.registry = planner, store, registry

    def from_text(self, text: str, context: str = "", adhoc: bool = False):
        """-> (task | None, problem). adhoc=True: a one-off job to run right now (manual trigger, not kept unless the user says so)."""
        if not self.planner.available:
            return None, "no language model is connected"
        sysm = ("You set up a new automated task from the user's request. " + TASK_DOC +
                '\nReply with ONE JSON object: {"task": {... all fields except lessons/state/id; "plan":{"mode":"adaptive","steps":[]}; '
                '"inputs" ONLY for a value the task cannot run without that the user has not given and that is not an obvious default (for example a person\'s name or an account); never for repeat counts, durations, "how long", "how many times", times or anything the request already settles - "every 3 minutes" simply keeps going; "values" for any the user already gave}, "id_hint":"short_snake_case"}. '
                "Pick a trigger that matches how often/when they want it (manual if unspecified)."
                + (" This is a ONE-OFF job to start right now: use the manual trigger and put the whole job in 'objective'. "
                                 "Do NOT create inputs for choices that have a sensible default (latest version, default location, usual settings): "
                                 "decide yourself and write the decision into the objective. Only create an input when the job truly cannot start "
                                 "without something only the user knows (a name, an account, a choice with no good default)." if adhoc else ""))
        err = ""
        for tier in (0, 0, 1):
            prompt = f"Now: {datetime.now():%A %Y-%m-%d %H:%M}\nContext: {context}\nUser request: {text}" + (f"\n\nRejected: {err}\nReply with the corrected JSON." if err else "")
            try:
                obj = xa.extract_json(self.planner._call(sysm, prompt, tier), key="task")
            except Exception as e:  # noqa: BLE001
                err = f"model error: {str(e)[:80]}"
                continue
            if not obj or not isinstance(obj.get("task"), dict):
                err = "no JSON object with a 'task' key"
                continue
            t = obj["task"]
            base = re.sub(r"[^a-z0-9]+", "_", str(obj.get("id_hint") or t.get("name", "task")).lower()).strip("_")[:30] or "task"
            tid, n = base, 2
            while tid in self.store.ids():
                tid, n = f"{base}_{n}", n + 1
            t["id"] = tid
            t.setdefault("plan", {"mode": "adaptive", "steps": []})
            t["plan"].setdefault("mode", "adaptive")
            t.setdefault("created_by", "user")
            t.setdefault("options", {}).setdefault("profile", "auto")
            if adhoc:
                t["trigger"] = {"type": "manual"}
                t["ephemeral"] = True
                t["created_at"] = time.time()
            clean, problems = validate_task(t, self.registry)
            if not problems:
                return clean, ""
            err = "; ".join(problems[:5])
        return None, err
