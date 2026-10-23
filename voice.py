from __future__ import annotations

import re
import time

_NUM = re.compile(r"\d+(?:[.:]\d+)*")
_DOMAIN = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)
_QUOTED = re.compile(r"'([^']{2,60})'")

KEY_KINDS = {"question", "summary", "error", "ack"}


class Voice:
    def __init__(self, rephrase_fn=None, mode: str = "key", log=print, cooldown_s: float = 60):
        """mode: 'off' (plain text), 'key' (questions, summaries, errors, acknowledgements), 'all'."""
        self.rephrase_fn, self.mode, self.log, self.cooldown_s = rephrase_fn, mode, log, cooldown_s
        self._cache: dict = {}
        self._down_until = 0.0
        self.calls = 0

    @staticmethod
    def facts(text: str, must=()) -> set:
        f = {m.group(0).lower() for m in _NUM.finditer(text)}
        f |= {m.group(0).lower() for m in _DOMAIN.finditer(text)}
        f |= {m.group(1).lower() for m in _QUOTED.finditer(text)}
        f |= {str(x).lower() for x in must if x}
        return f

    def ok(self, original: str, new: str, must=()) -> bool:
        new = (new or "").strip()
        if not new or "\n" in new.strip():
            return False
        if len(new) > 2.5 * len(original) + 60:
            return False
        if len(new) < 0.45 * len(original):                                  # a rewrite must not shrink to a bare fact
            return False
        if "?" in original and "?" not in new:                               # a question has to stay a question
            return False
        bare = re.sub(r"[A-Za-z]:\\\S*|https?://\S+|\S+\.\w{2,5}\b|'[^']*'|\d[\d.:]*", " ", new)
        if len(re.findall(r"[A-Za-z]{2,}", bare)) < min(3, len(re.findall(r"[A-Za-z]{2,}", re.sub(r"[A-Za-z]:\\\S*|https?://\S+|\S+\.\w{2,5}\b|'[^']*'|\d[\d.:]*", " ", original)))):
            return False                                                      # nothing left but names/paths/numbers
        low = new.lower()
        if any(f not in low for f in self.facts(original, must)):           # something was dropped
            return False
        if {m.group(0).lower() for m in _NUM.finditer(new)} - {m.group(0).lower() for m in _NUM.finditer(original)}:
            return False                                                      # a number was invented
        if {m.group(0).lower() for m in _DOMAIN.finditer(new)} - {m.group(0).lower() for m in _DOMAIN.finditer(original)}:
            return False                                                      # a site was invented
        return True

    def say(self, text: str, kind: str = "info", must=()) -> str:
        if (self.mode == "off" or not self.rephrase_fn or not text
                or (self.mode == "key" and kind not in KEY_KINDS)):
            return text
        key = (kind, text, tuple(must))
        if key in self._cache:
            return self._cache[key]
        if time.time() < self._down_until:
            return text
        try:
            self.calls += 1
            new = str(self.rephrase_fn(text, kind) or "").strip().strip('"')
        except Exception as e:  # noqa: BLE001  (rate limit, model down...) -> plain text, and back off for a while
            self._down_until = time.time() + self.cooldown_s
            self.log(f"[VOICE] rephrase unavailable ({type(e).__name__}) - using plain text for a while")
            return text
        out = new if self.ok(text, new, must) else text
        if len(self._cache) > 300:
            self._cache.clear()
        self._cache[key] = out
        return out
