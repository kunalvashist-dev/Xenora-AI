"""
xenora_skillhub.py - find skills on GitHub, read them, and turn them into notes she can use.

A "skill" on GitHub is usually a folder with a SKILL.md (a how-to written for an AI assistant), often a whole pack of them. This
module can

    search(query)               -> repositories that look relevant (GitHub's public search)
    learn(url, focus="")        -> fetch the file / folder / repository, pick the relevant skills, and save each as a NOTE

Safety, because this reads text from the internet:
  * only github.com / raw.githubusercontent.com / api.github.com are ever contacted (a file or folder on this PC can also be learned);
  * downloaded text is treated as untrusted DATA: it is never executed, and it is not stored as-is - a model rewrites it into a short
    how-to note (the instructions to that model say to ignore anything addressed to an AI or asking for commands/data);
  * the notes are saved as "imported, not yet reviewed" and shown in the TASKS tab so you can read, edit or delete them;
  * a note is guidance she may follow or ignore. It can't change her code, her permissions or her safety rules.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_HOSTS = {"github.com", "www.github.com", "raw.githubusercontent.com", "api.github.com"}
MAX_FILE = 9000                      # characters read from any one file
_HEADERS = {"User-Agent": "Xenora-skill-reader", "Accept": "application/vnd.github+json"}


class HubError(Exception):
    pass


def _http(url: str, timeout: float = 20.0) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise HubError(f"only GitHub addresses are allowed (not {host or url})")
    headers = dict(_HEADERS)
    if os.environ.get("GITHUB_TOKEN") and host == "api.github.com":
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]          # optional: lifts GitHub's 60-requests-an-hour limit
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise HubError("GitHub is rate-limiting me right now - try again in a few minutes") from e
        raise HubError(f"GitHub said {e.code} for that address") from e
    except urllib.error.URLError as e:
        raise HubError(f"couldn't reach GitHub ({e.reason})") from e


def parse(url: str) -> dict:
    """github.com/o/r | /blob/ref/path | /tree/ref/path | raw.githubusercontent.com/o/r/ref/path"""
    u = urllib.parse.urlparse(url.strip())
    host, parts = (u.hostname or ""), [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if host not in ALLOWED_HOSTS:
        raise HubError("that isn't a GitHub address")
    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise HubError("that raw address is incomplete")
        return {"owner": parts[0], "repo": parts[1], "ref": parts[2], "path": "/".join(parts[3:]), "kind": "blob"}
    if len(parts) < 2:
        raise HubError("I need a repository address like github.com/owner/repo")
    out = {"owner": parts[0], "repo": parts[1].removesuffix(".git"), "ref": "", "path": "", "kind": "repo"}
    if len(parts) >= 4 and parts[2] in ("blob", "tree"):
        out.update(kind=parts[2], ref=parts[3], path="/".join(parts[4:]))
    return out


def _default_branch(o: str, r: str) -> str:
    return json.loads(_http(f"https://api.github.com/repos/{o}/{r}")).get("default_branch") or "main"


def _tree(o: str, r: str, ref: str) -> list:
    data = json.loads(_http(f"https://api.github.com/repos/{o}/{r}/git/trees/{urllib.parse.quote(ref)}?recursive=1"))
    return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]


def _raw(o: str, r: str, ref: str, path: str) -> str:
    return _http(f"https://raw.githubusercontent.com/{o}/{r}/{urllib.parse.quote(ref)}/{urllib.parse.quote(path)}")[:MAX_FILE]


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())}


_OVERVIEW = re.compile(r"director|orchestrat|index|overview|guide|core|getting-started", re.I)


def collect(url: str, focus: str = "", limit: int = 5) -> dict:
    """-> {"source": url, "files": [{"path","text"}], "available": [skill folder names not read]}"""
    p = parse(url)
    o, r = p["owner"], p["repo"]
    if p["kind"] == "blob":
        ref = p["ref"] or "HEAD"
        return {"source": url, "files": [{"path": p["path"], "text": _raw(o, r, ref, p["path"])}], "available": []}
    ref = p["ref"] or "HEAD"
    start = p["path"].strip("/")
    paths = [x for x in _tree(o, r, ref) if not start or x == start or x.startswith(start + "/")]
    skills = [x for x in paths if x.split("/")[-1].lower() == "skill.md"]
    if not skills:                                                     # not a skill pack: read its README(s) instead
        readmes = sorted([x for x in paths if x.split("/")[-1].lower() in ("readme.md", "readme.rst", "readme")], key=len)
        if not readmes:
            raise HubError("I found no SKILL.md or README there")
        return {"source": url, "files": [{"path": readmes[0], "text": _raw(o, r, ref, readmes[0])}], "available": []}
    want = _words(focus)

    def score(x):
        d = x.rsplit("/", 2)[-2] if "/" in x else x
        return (len(want & _words(d.replace("-", " "))) * 3 + (1 if _OVERVIEW.search(d) else 0), -len(x))
    ranked = sorted(skills, key=score, reverse=True)
    pick = ranked[:limit]
    names = [x.rsplit("/", 2)[-2] if "/" in x else x for x in ranked[limit:]]
    return {"source": url, "files": [{"path": x, "text": _raw(o, r, ref, x)} for x in pick], "available": names[:60]}


def collect_local(path: str, focus: str = "", limit: int = 5) -> dict:
    """A skill that lives on THIS computer: one file (SKILL.md / any .md or .txt), or a folder of them."""
    p = os.path.expanduser(str(path).strip().strip('"'))
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return {"source": p, "local": True, "files": [{"path": os.path.basename(p), "text": f.read(MAX_FILE)}], "available": []}
    if not os.path.isdir(p):
        raise HubError(f"'{path}' isn't a GitHub address and isn't a file or folder on this PC")
    found = []
    for dp, dns, fns in os.walk(p):
        dns[:] = [d for d in dns if not d.startswith(".") and d.lower() not in ("node_modules", "__pycache__")]
        if dp[len(p):].count(os.sep) >= 4:
            dns[:] = []
        for fn in fns:
            if fn.lower().endswith((".md", ".txt")):
                found.append(os.path.join(dp, fn))
    skills = [x for x in found if os.path.basename(x).lower() == "skill.md"] or found
    if not skills:
        raise HubError("I found no .md or .txt notes in that folder")
    want = _words(focus)
    ranked = sorted(skills, key=lambda x: (len(want & _words(os.path.relpath(x, p).replace(os.sep, " "))) * 3 + (1 if _OVERVIEW.search(x) else 0), -len(x)), reverse=True)
    out = []
    for x in ranked[:limit]:
        with open(x, "r", encoding="utf-8", errors="replace") as f:
            out.append({"path": os.path.relpath(x, p), "text": f.read(MAX_FILE)})
    return {"source": p, "local": True, "files": out, "available": [os.path.relpath(x, p) for x in ranked[limit:]][:60]}


def search(query: str, n: int = 5) -> list:
    q = urllib.parse.quote(f"{query} skill in:name,description,topics,readme")
    data = json.loads(_http(f"https://api.github.com/search/repositories?q={q}&per_page={max(1, min(int(n), 8))}"))
    return [{"name": it["full_name"], "url": it["html_url"], "stars": it.get("stargazers_count", 0),
             "about": (it.get("description") or "")[:160]} for it in data.get("items", [])]


class SkillHub:
    def __init__(self, skills, distill, tools=lambda: [], log=print):
        """distill(text, source, tools) -> {"name","when","keywords","tips"} | None   (a model call; see Planner.distill_skill)"""
        self.skills, self.distill, self.tools, self.log = skills, distill, tools, log

    def find(self, query: str) -> dict:
        try:
            return {"ok": True, "data": {"text": "; ".join(f"{x['name']} ({x['stars']}★) - {x['about']} <{x['url']}>" for x in search(query)) or "nothing found",
                                         "results": search(query)}}
        except HubError as e:
            return {"ok": False, "error": str(e)}

    def learn(self, url: str, focus: str = "", limit: int = 5) -> dict:
        try:
            u = str(url).strip().strip('"')
            local = os.path.exists(os.path.expanduser(u)) or not re.match(r"^https?://", u, re.I)       # a path on this PC, or something that isn't a web address
            got = collect_local(u, focus, limit) if local else collect(u, focus, limit)
        except HubError as e:
            return {"ok": False, "error": str(e)}
        learned, skipped = [], 0
        for f in got["files"]:
            try:
                note = self.distill(f["text"], f"{got['source']} ({f['path']})", list(self.tools()))
            except Exception as e:  # noqa: BLE001
                self.log(f"[SKILLHUB] couldn't distil {f['path']}: {e}")
                note = None
            if note and note.get("name") and isinstance(note.get("tips"), list) and note["tips"]:
                slug = self.skills.add_imported(note["name"], note.get("when", ""), note["tips"], note.get("keywords") or [],
                                                f"{got['source']} ({f['path']})", reviewed="yes" if got.get("local") else "no")   # your own file is trusted
                learned.append(note["name"])
                self.log(f"[SKILLHUB] learned '{note['name']}' -> {slug}")
            else:
                skipped += 1
        text = f"learned {len(learned)} skill{'s' if len(learned) != 1 else ''}: {', '.join(learned) or 'none'}"
        if got["available"]:
            text += f". The pack has {len(got['available'])} more ({', '.join(got['available'][:12])}...) - ask for one by name to learn it too"
        return {"ok": bool(learned), "error": None if learned else "none of it contained usable how-to knowledge",
                "data": {"learned": learned, "skipped": skipped, "available": got["available"], "text": text}}
