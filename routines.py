from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta


class Scheduler:
    def __init__(self, state_path: str, *, now=None, window_titles=None, tick_s: float = 5, log=print,
                 on_error=None, on_wait=None):
        """on_error(rid, exc): a routine crashed (tell the user - never fail silently).
           on_wait(rid): a requested routine has to queue behind another one that is still running."""
        self.on_error, self.on_wait = on_error, on_wait
        self.started = False
        self._active: set = set()                   # routines running or queued (so a repeated command doesn't double-run)
        self.path, self._now = state_path, now or datetime.now
        self._titles = window_titles or (lambda: [])
        self.tick_s, self.log = tick_s, log
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._daily: dict[str, dict] = {}       # rid -> {"fn": callable, "retry": min, "max": n}
        self._triggers: dict[str, dict] = {}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.state = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"daily": {}}

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def add_daily(self, rid: str, hhmm: str, fn, *, retry_minutes: int = 30, max_attempts: int = 3):
        """Kept for callers that only know daily-at-a-time. Everything now goes through add_schedule()."""
        return self.add_schedule(rid, {"type": "daily", "time": hhmm}, fn, retry_minutes=retry_minutes, max_attempts=max_attempts)

    def add_schedule(self, rid: str, spec: dict, fn, *, retry_minutes: int = 30, max_attempts: int = 3):
        """spec is the task's own trigger, as data:
             {"type":"daily","time":"HH:MM"}
             {"type":"weekly","days":["mon","thu"],"time":"HH:MM"}
             {"type":"interval","every_minutes":120}
             {"type":"once","at":"2026-10-20T09:30"}
           fn() -> {"complete": bool, ...}. The task is the source of truth: re-registering updates the schedule."""
        spec = dict(spec or {})
        with self._lock:
            st = self.state["daily"].setdefault(rid, {"time": spec.get("time", "00:00"), "enabled": True, "last_complete": None,
                                                      "attempts_date": None, "attempts": 0, "next_try": None})
            if st.get("spec") != spec:                       # the schedule changed: start that new schedule cleanly
                st["spec"] = spec
                st["time"] = spec.get("time", st.get("time", "00:00"))
                st["last_run"] = self._now().isoformat() if spec.get("type") == "interval" else None
                if spec.get("type") == "once":
                    st["last_complete"] = None
                st["next_try"] = None
            st["enabled"] = True
            self._daily[rid] = {"fn": fn, "retry": retry_minutes, "max": max_attempts}
            self._save()

    def add_window_trigger(self, rid: str, title_rx: str, fn, *, absent_s: float = 30,
                           fire_if_open_at_start: bool = True):
        self._triggers[rid] = {"rx": re.compile(title_rx, re.I), "fn": fn, "absent_s": absent_s,
                               "fire_open": fire_if_open_at_start, "present": False, "absent_since": None,
                               "first": True}

    def set_time(self, rid: str, hhmm: str):
        with self._lock:
            self.state["daily"][rid]["time"] = hhmm
            self._save()

    def skip_today(self, rid: str):
        with self._lock:
            self.state["daily"][rid]["last_complete"] = self._now().date().isoformat()
            self._save()

    def remove(self, rid: str):
        """Stop scheduling a task (it stays known, just not triggered). Used when a task is paused, deleted or made manual."""
        with self._lock:
            self._daily.pop(rid, None)
            self._triggers.pop(rid, None)
            if rid in self.state.get("daily", {}):
                self.state["daily"][rid]["enabled"] = False
                self._save()

    def set_enabled(self, rid: str, on: bool):
        with self._lock:
            self.state["daily"][rid]["enabled"] = on
            self._save()

    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.state))

    DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def _spec(self, rid: str) -> dict:
        d = self.state["daily"][rid]
        return d.get("spec") or {"type": "daily", "time": d.get("time", "09:00")}

    def _due(self, rid: str, now: datetime) -> bool:
        d = self.state["daily"][rid]
        if not d["enabled"]:
            return False
        spec = self._spec(rid)
        typ, today = spec.get("type", "daily"), now.date().isoformat()
        if d.get("attempts_date") != today:                  # a new day: fresh attempts for every kind of schedule
            d.update(attempts_date=today, attempts=0, next_try=None)
        if d.get("next_try") and now < datetime.fromisoformat(d["next_try"]):
            return False
        if typ == "interval":
            try:
                last = datetime.fromisoformat(d.get("last_run")) if d.get("last_run") else None
            except ValueError:
                last = None
            every = max(1.0, float(spec.get("every_minutes", 60)))
            if last is not None and now < last + timedelta(minutes=every):
                return False
            return d.get("attempts", 0) < self._daily[rid]["max"]
        if typ == "once":
            try:
                at = datetime.fromisoformat(str(spec.get("at")))
            except ValueError:
                return False
            if d.get("last_complete") or now < at or now > at + timedelta(days=1):
                return False
        else:
            if d.get("last_complete") == today:
                return False
            if typ == "weekly":
                days = [str(x).lower()[:3] for x in (spec.get("days") or [])]
                if days and self.DAYS[now.weekday()] not in days:
                    return False
            hh, mm = (int(x) for x in str(spec.get("time", d.get("time", "09:00"))).split(":"))
            if now < now.replace(hour=hh, minute=mm, second=0, microsecond=0):
                return False
        if d.get("attempts_date") != today:
            d.update(attempts_date=today, attempts=0, next_try=None)
        return d["attempts"] < self._daily[rid]["max"]

    def next_run(self, rid: str):
        """When this schedule fires next (datetime) or None - for the UI."""
        with self._lock:
            d = self.state["daily"].get(rid)
            if not d or not d.get("enabled") or rid not in self._daily:
                return None
            now, spec = self._now(), self._spec(rid)
            typ = spec.get("type", "daily")
            try:
                if typ == "interval":
                    last = datetime.fromisoformat(d["last_run"]) if d.get("last_run") else now
                    return max(now, last + timedelta(minutes=float(spec.get("every_minutes", 60))))
                if typ == "once":
                    at = datetime.fromisoformat(str(spec.get("at")))
                    return None if d.get("last_complete") else at
                hh, mm = (int(x) for x in str(spec.get("time", "09:00")).split(":"))
                days = [str(x).lower()[:3] for x in (spec.get("days") or [])] if typ == "weekly" else []
                for add in range(0, 8):
                    cand = (now + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if cand < now or (add == 0 and d.get("last_complete") == now.date().isoformat()):
                        continue
                    if days and self.DAYS[cand.weekday()] not in days:
                        continue
                    return cand
            except Exception:
                return None
        return None

    def _report(self, rid: str, exc: Exception):
        self.log(f"[ROUTINE] {rid} crashed: {type(exc).__name__}: {exc}")
        if self.on_error:
            try:
                self.on_error(rid, exc)
            except Exception:
                pass

    def _run_daily(self, rid: str, forced: bool = False):
        if rid in self._active:
            if forced and self.on_wait:
                self.on_wait(rid)                    # "already on it" - don't start a second copy
            return
        self._active.add(rid)
        got = self._run_lock.acquire(blocking=False)
        if not got:
            if not forced:
                self._active.discard(rid)
                return                               # scheduled pass: another routine is running, try next tick
            if self.on_wait:
                self.on_wait(rid)                    # you ASKED for it: queue up, don't silently drop the request
            self._run_lock.acquire()
        try:
            with self._lock:
                d = self.state["daily"][rid]
                today = self._now().date().isoformat()
                if d.get("attempts_date") != today:
                    d.update(attempts_date=today, attempts=0)
                d["attempts"] += 1
                self._save()
            try:
                res = self._daily[rid]["fn"]() or {}
            except Exception as e:  # noqa: BLE001
                self._report(rid, e)
                res = {"complete": False}
            with self._lock:
                if res.get("complete"):
                    d["last_complete"] = today
                    d["next_try"] = None
                    d["attempts"] = 0
                    d["last_run"] = self._now().isoformat()
                    if self._spec(rid).get("type") == "once":
                        d["enabled"] = False               # a one-shot is finished for good
                else:
                    d["next_try"] = (self._now() + timedelta(minutes=self._daily[rid]["retry"])).isoformat()
                self._save()
        finally:
            self._active.discard(rid)
            self._run_lock.release()

    def _spawn(self, target, *args):
        t = threading.Thread(target=target, args=args, daemon=True, name="assistant-routine")
        self._threads.append(t)
        t.start()
        return t

    def run_now(self, rid: str):
        return self._spawn(self._run_daily, rid, True)

    def tick(self):
        """One scheduling pass (called every tick_s by the loop; callable directly in tests)."""
        now = self._now()
        with self._lock:
            due = [rid for rid in self._daily if self._due(rid, now)]
        for rid in due:
            if not self._run_lock.locked():
                self._spawn(self._run_daily, rid)
        try:
            titles = self._titles()
        except Exception:
            titles = []
        for rid, tr in self._triggers.items():
            matched = any(tr["rx"].search(t) for t in titles)
            if matched and not tr["present"]:
                tr["present"], tr["absent_since"] = True, None
                if tr["first"] and not tr["fire_open"]:
                    pass
                else:
                    self._spawn(self._safe, rid, tr["fn"])
            elif not matched and tr["present"]:
                tr["absent_since"] = tr["absent_since"] or time.time()
                if time.time() - tr["absent_since"] >= tr["absent_s"]:
                    tr["present"] = False
            elif matched:
                tr["absent_since"] = None
            tr["first"] = False

    def _safe(self, rid, fn):
        """Triggers take the SAME floor as daily routines: two routines must never ask the user questions at once
        (the Minecraft-watch question used to cut into the middle of the vote's own questions)."""
        if not self._run_lock.acquire(timeout=1800):
            self.log(f"[TRIGGER] {rid}: gave up waiting for another routine to finish")
            return
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self._report(rid, e)
        finally:
            self._run_lock.release()

    def start(self):
        self.started = True

        def loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[SCHED] tick error: {e}")
                self._stop.wait(self.tick_s)
        self._spawn(loop)
        return self

    def stop(self):
        self._stop.set()
