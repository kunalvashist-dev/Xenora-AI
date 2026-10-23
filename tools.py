from __future__ import annotations

import functools
import http.server
import os
import re
import shlex
import shutil
import socketserver
import subprocess
import threading
import time

MAX_FILE_CHARS = 200_000

SHELL_POLICY = {
    "deny": ["rm", "rmdir", "del", "erase", "format", "shutdown", "reboot", "restart-computer", "reg", "regedit",
             "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "sudo", "su", "chmod", "chown", "taskkill", "kill",
             "net", "sc", "schtasks", "diskpart", "bcdedit", "cipher", "takeown", "icacls", "env", "printenv", "set",
             "curl", "wget", "iwr", "invoke-webrequest", "certutil", "bitsadmin", "mshta", "wscript", "cscript",
             "rundll32", "msiexec", "dd", "mkfs", "mount"],
    "tools": ["git", "netlify", "vercel", "npm", "pip", "pip3", "gh", "wrangler", "flyctl", "firebase", "surge",
              "heroku", "railway", "hugo", "tsc", "vite"],
}
_SHELL_META = re.compile(r"[|&;<>`\n\r]|\$\(")
_SECRET_LINE = re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|authorization|bearer)\s*[:=]?\s*)([^\s\"']{6,})")


def _exe_name(p: str) -> str:
    b = os.path.basename(p).lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if b.endswith(ext):
            return b[: -len(ext)]
    return b


def _split_command(cmd: str):
    parts = shlex.split(cmd, posix=(os.name != "nt"))
    if os.name == "nt":
        parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return parts


def redact(text: str) -> str:
    return _SECRET_LINE.sub(lambda m: m.group(1) + "[hidden]", text or "")


class TemplateError(Exception):
    pass


class ConditionError(Exception):
    pass


_TPL = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def lookup(path: str, ctx):
    """'s1.data.price', 'inputs.sites[0]' -> value. Raises TemplateError with the missing part."""
    cur = ctx
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        try:
            if part.startswith("["):
                cur = cur[int(part[1:-1])]
            elif isinstance(cur, dict):
                cur = cur[part]
            else:
                cur = getattr(cur, part)
        except (KeyError, IndexError, AttributeError, TypeError):
            raise TemplateError(f"'{path}' is not available (stopped at '{part}')")
    return cur


def render(obj, ctx):
    """Replace {{refs}} anywhere inside strings/lists/dicts. A string that is ONLY one ref keeps its real type."""
    if isinstance(obj, str):
        m = _TPL.fullmatch(obj.strip())
        if m:
            return lookup(m.group(1), ctx)
        return _TPL.sub(lambda mm: str(lookup(mm.group(1), ctx)), obj)
    if isinstance(obj, list):
        return [render(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: render(v, ctx) for k, v in obj.items()}
    return obj


def refs(obj) -> set:
    """Root names a structure refers to ({{s1.x}} -> 's1'). Used to check a plan only uses EARLIER results."""
    out = set()
    if isinstance(obj, str):
        for m in _TPL.finditer(obj):
            out.add(re.split(r"[.\[]", m.group(1).strip())[0])
    elif isinstance(obj, list):
        for x in obj:
            out |= refs(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= refs(v)
    return out


def to_num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(x))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


_OPS = {"==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "exists", "not_exists", "matches"}


def eval_condition(cond, ctx) -> bool:
    """{"var": "s1.answer", "op": "<", "value": 100} | {"all":[...]} | {"any":[...]} | {"not": {...}}.
    A variable that doesn't exist makes the condition False (except not_exists)."""
    if not isinstance(cond, dict):
        raise ConditionError("a condition must be an object")
    if "all" in cond:
        return all(eval_condition(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(eval_condition(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not eval_condition(cond["not"], ctx)
    op = cond.get("op")
    if op not in _OPS or "var" not in cond:
        raise ConditionError(f"bad condition {cond!r}")
    path = str(cond["var"]).strip()
    path = _TPL.fullmatch(path).group(1).strip() if _TPL.fullmatch(path) else path
    try:
        left = lookup(path, ctx)
    except TemplateError:
        return op == "not_exists"
    if op == "exists":
        return left not in (None, "", [], {})
    if op == "not_exists":
        return left in (None, "", [], {})
    right = render(cond.get("value"), ctx)
    if op in ("==", "!="):
        ln, rn = to_num(left), to_num(right)
        numeric = ln is not None and rn is not None and (
            isinstance(left, (int, float)) or re.fullmatch(r"\s*[$\u20ac\u00a3]?\s*-?[\d,.]+\s*", str(left)))
        eq = (ln == rn) if numeric else str(left).strip().lower() == str(right).strip().lower()
        return eq if op == "==" else not eq
    if op in (">", ">=", "<", "<="):
        ln, rn = to_num(left), to_num(right)
        if ln is None or rn is None:
            return False
        return {">": ln > rn, ">=": ln >= rn, "<": ln < rn, "<=": ln <= rn}[op]
    if op == "contains":
        return str(right).lower() in (str(left).lower() if not isinstance(left, list) else " ".join(map(str, left)).lower())
    if op == "not_contains":
        return str(right).lower() not in str(left).lower()
    if op == "matches":
        try:
            return bool(re.search(str(right), str(left), re.I))
        except re.error:
            raise ConditionError(f"bad pattern {right!r}")
    return False


class Tool:
    def __init__(self, name, desc, fn, params=None, required=(), risk="read", recovery=False,
                 available=None, approval=None, precheck=None):
        self.name, self.desc, self.fn = name, desc, fn
        self.params = dict(params or {})
        self.required = tuple(required)
        self.risk = risk                  # read | act | irreversible
        self.recovery = recovery          # may be used to repair a failure (restart a browser, wait...)
        self.available = available        # () -> (bool, reason)
        self.approval = approval          # args -> {domain, op, label, value, desc[, force_ask]}  (how a permission is keyed/worded)
        self.precheck = precheck          # args -> error text | None   (refuse BEFORE bothering the user with a question)


SENSITIVE_DIRS = {"appdata", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", "keepass", "1password", "bitwarden", "$recycle.bin",
                  "system volume information", "programdata"}
SENSITIVE_FILES = re.compile(r"(\.kdbx?$|^id_(rsa|ed25519|ecdsa)|\.(pem|pfx|p12|ppk|key)$|^\.env|^credentials|^cookies|^login data|^ntuser\.dat|\.wallet$)", re.I)


class ToolRegistry:
    def __init__(self):
        self._t: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._t[tool.name] = tool
        return tool

    def get(self, name: str):
        return self._t.get(name)

    def usable(self, name: str) -> bool:
        t = self._t.get(name)
        if not t:
            return False
        if t.available:
            try:
                return bool(t.available()[0])
            except Exception:
                return False
        return True

    def names(self, recovery_only: bool = False) -> list:
        return [n for n, t in self._t.items() if self.usable(n) and (t.recovery or not recovery_only)]

    def catalog(self, recovery_only: bool = False) -> str:
        lines = []
        for n in self.names(recovery_only):
            t = self._t[n]
            ps = ", ".join(p + ("" if p in t.required else "?") for p in t.params)
            lines.append(f"- {n}({ps}) [{t.risk}]: {t.desc}")
        return "\n".join(lines)

    def validate_args(self, name: str, args: dict) -> list:
        t = self._t.get(name)
        if not t:
            return [f"unknown tool '{name}'"]
        if not self.usable(name):
            return [f"tool '{name}' is not available right now"]
        errs = []
        for k in args:
            if k not in t.params:
                errs.append(f"tool '{name}' has no parameter '{k}' (it takes: {', '.join(t.params) or 'none'})")
        for k in t.required:
            if k not in args:
                errs.append(f"tool '{name}' needs '{k}'")
        return errs

    def call(self, name: str, args: dict) -> dict:
        t = self._t.get(name)
        if not t:
            return {"ok": False, "error": f"unknown tool '{name}'", "data": None, "needs_human": None, "halted": False}
        try:
            r = t.fn(**(args or {}))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "data": None,
                    "needs_human": None, "halted": False}
        if isinstance(r, dict) and "ok" in r:
            r.setdefault("data", None)
            r.setdefault("error", None)
            r.setdefault("needs_human", None)
            r.setdefault("halted", False)
            return r
        return {"ok": True, "data": r, "error": None, "needs_human": None, "halted": False}


def build_registry(*, hands=None, search_fn=None, open_app_fn=None, notify_fn=None, ask_fn=None, mem_search=None,
                   mem_add=None, watch_fn=None, extract_fn=None, files_dir: str = ".", extra_roots=(),
                   is_blocked_path=None, shell_policy=None, read_anywhere: bool = False, log=print) -> ToolRegistry:
    reg = ToolRegistry()
    policy = {**SHELL_POLICY, **(shell_policy or {})}
    servers: dict = {}
    files_dir = os.path.abspath(files_dir)
    roots = [files_dir] + [os.path.abspath(r) for r in extra_roots if r]

    def _sensitive(full: str) -> bool:
        """Places she never reads even in read-anywhere mode: credentials, key stores, browser data, app data."""
        low = full.replace("\\", "/").lower()
        parts = set(low.split("/"))
        return bool(parts & SENSITIVE_DIRS) or bool(SENSITIVE_FILES.search(low.rsplit("/", 1)[-1]))

    def safe_path(p: str, read: bool = False):
        """Only PROTECTED places (system folders, her own install, credentials, browser/app data) are ever refused. Looking at things,
        and creating new things, work anywhere else. What is gated is CHANGING something that already exists outside the folders she
        works in: that goes through a permission question (see _with_target)."""
        p = os.path.expanduser(str(p or "").strip())
        if not p:
            raise ValueError("empty path")
        full = os.path.realpath(p if os.path.isabs(p) else os.path.join(files_dir, p))
        if (is_blocked_path and is_blocked_path(full)) or _sensitive(full):
            raise PermissionError("that location is protected")
        return full

    def inside(full: str) -> bool:
        """Is this inside a folder she works in (her own folder, or a workspace you gave her)?"""
        for r in roots:
            rr = os.path.realpath(r)
            try:
                if os.path.commonpath([full, rr]) == rr:
                    return True
            except ValueError:
                continue
        return False

    def _looks_text(full: str) -> bool:
        with open(full, "rb") as f:
            head = f.read(4096)
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
            return True
        except UnicodeDecodeError:
            ok = sum(1 for c in head if c in (9, 10, 13) or 32 <= c < 127 or c >= 160)
            return not head or ok / len(head) >= 0.95

    def read_file(path):
        full = safe_path(path, read=True)
        if not os.path.isfile(full):
            raise FileNotFoundError(f"'{path}' isn't a file")
        if _looks_text(full):                                       # source code, scripts, notes, configs: just read them
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return f.read(MAX_FILE_CHARS)
        if extract_fn:                                              # documents (pdf / docx / xlsx ...) go to the extractor
            res = extract_fn(full)
            txt, err = res if isinstance(res, tuple) else (res, None)
            if txt:
                return str(txt)[:MAX_FILE_CHARS]
            raise ValueError(err or f"I can't read {os.path.splitext(full)[1] or 'that kind of'} files as text")
        raise ValueError("that is a binary file - I can't read it as text")

    def _write(path, text, mode, must_not_exist=False):
        full = safe_path(path)
        text = str(text)
        if len(text) > MAX_FILE_CHARS:
            raise ValueError(f"too long to write ({len(text)} characters; the limit is {MAX_FILE_CHARS})")
        if must_not_exist and os.path.exists(full):
            raise FileExistsError(f"'{os.path.basename(full)}' already exists - I won't overwrite it without asking")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, mode, encoding="utf-8") as f:
            f.write(text)
        return {"path": full, "chars": len(text)}

    reg.safe_path = safe_path                      # exposed so wrapped abilities obey the same sandbox
    reg.roots = roots                              # the folders she works in (workspaces): changes there need no extra questions
    reg.inside = inside

    def add_root(path):
        """Allow one more folder (only ever called because the user named it). Specific folders only; protected places are refused."""
        full = os.path.realpath(os.path.expanduser(str(path).strip()))
        if not os.path.isdir(full):
            raise ValueError(f"'{path}' isn't a folder")
        if len(full.rstrip("\\/")) <= 3 or full == os.path.realpath(os.path.expanduser("~")):
            raise PermissionError("that is too broad - name a specific folder")
        if is_blocked_path and is_blocked_path(full):
            raise PermissionError("that location is protected")
        if full not in [os.path.realpath(r) for r in roots]:
            roots.append(full)
        return full
    reg.add_root = add_root
    short = lambda s, n=40: str(s)[:n]
    reg.register(Tool("read_file", "Read a text file (inside my allowed folders).", read_file, {"path": "file path"},
                      ["path"]))
    reg.register(Tool("write_file", "Create a NEW text file (refuses to overwrite).",
                      lambda path, text: _write(path, text, "w", True), {"path": "file path", "text": "content"},
                      ["path", "text"], risk="act",
                      approval=lambda a: {"domain": "files", "op": "write", "label": f"write file {short(os.path.basename(str(a.get('path'))))}",
                                          "value": "", "desc": f"Create the file '{os.path.basename(str(a.get('path')))}'?"}))
    reg.register(Tool("append_file", "Add text to the end of a file (creates it if missing).",
                      lambda path, text: _write(path, text, "a"), {"path": "file path", "text": "content"},
                      ["path", "text"], risk="act",
                      approval=lambda a: {"domain": "files", "op": "append", "label": f"add to file {short(os.path.basename(str(a.get('path'))))}",
                                          "value": "", "desc": f"Add to the file '{os.path.basename(str(a.get('path')))}'?"}))
    reg.register(Tool("overwrite_file", "Replace the contents of an existing file. DESTRUCTIVE.",
                      lambda path, text: _write(path, text, "w"), {"path": "file path", "text": "content"},
                      ["path", "text"], risk="irreversible",
                      approval=lambda a: {"domain": "files", "op": "overwrite", "label": f"overwrite file {short(os.path.basename(str(a.get('path'))))}",
                                          "value": "", "desc": f"Overwrite the file '{os.path.basename(str(a.get('path')))}' (the old contents are lost)?"}))


    def edit_file(path, old, new):
        full = safe_path(path)
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        n = txt.count(str(old))
        if n != 1:
            raise ValueError(f"the text to replace appears {n} times (it must appear exactly once - give more surrounding text)")
        out = txt.replace(str(old), str(new), 1)
        if len(out) > MAX_FILE_CHARS:
            raise ValueError("the result would be too long")
        with open(full, "w", encoding="utf-8") as f:
            f.write(out)
        return {"path": full, "chars": len(out)}

    def list_dir(path=".", sort="name", limit=200):
        full = safe_path(path, read=True)
        if not os.path.isdir(full):
            raise NotADirectoryError(f"'{path}' is not a folder")
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 200
        rows = []
        for n in os.listdir(full):
            p = os.path.join(full, n)
            try:
                isf = os.path.isfile(p)
                rows.append({"name": n, "type": "file" if isf else "dir", "size": os.path.getsize(p) if isf else None,
                             "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))})
            except OSError:
                continue                                              # vanished or unreadable: skip it
        key = {"size": lambda r: -(r["size"] or 0), "modified": lambda r: r["modified"]}.get(str(sort).lower())
        if str(sort).lower() == "modified":
            rows.sort(key=lambda r: r["modified"], reverse=True)       # newest first
        else:
            rows.sort(key=key or (lambda r: r["name"].lower()))        # size: biggest first; default: by name
        return {"total": len(rows), "shown": min(limit, len(rows)), "items": rows[:limit]}

    def make_dir(path):
        full = safe_path(path)
        os.makedirs(full, exist_ok=True)
        return {"path": full}

    def move_file(src, dst):
        s_, d_ = safe_path(src), safe_path(dst)
        if s_ in [os.path.realpath(r) for r in roots]:
            raise PermissionError("I won't move a whole allowed folder")
        if os.path.exists(d_):
            raise FileExistsError(f"'{os.path.basename(d_)}' already exists")
        os.makedirs(os.path.dirname(d_), exist_ok=True)
        shutil.move(s_, d_)
        return {"from": s_, "to": d_}

    def copy_file(src, dst):
        s_, d_ = safe_path(src), safe_path(dst)
        if not os.path.isfile(s_):
            raise ValueError("copy_file only copies single files")
        if os.path.exists(d_):
            raise FileExistsError(f"'{os.path.basename(d_)}' already exists")
        os.makedirs(os.path.dirname(d_), exist_ok=True)
        shutil.copy2(s_, d_)
        return {"from": s_, "to": d_}

    def delete_file(path):
        full = safe_path(path)
        if full in [os.path.realpath(r) for r in roots]:
            raise PermissionError("I won't delete a whole allowed folder")
        if os.path.isdir(full) and os.listdir(full):
            raise OSError("that folder isn't empty - I only delete files and empty folders (delete the files first)")
        trash = os.path.join(files_dir, ".trash")
        os.makedirs(trash, exist_ok=True)
        dest = os.path.join(trash, f"{time.strftime('%Y%m%d-%H%M%S')}_{os.path.basename(full)}")
        shutil.move(full, dest)                                  # recoverable: nothing is permanently erased
        return {"deleted": full, "recoverable_at": dest}

    bn = lambda a, k="path": short(os.path.basename(str(a.get(k))))
    reg.register(Tool("edit_file", "Replace one exact piece of text in a file with other text (it must appear once).", edit_file,
                      {"path": "file path", "old": "exact text to replace", "new": "replacement text"}, ["path", "old", "new"], risk="act",
                      approval=lambda a: {"domain": "files", "op": "edit", "label": f"edit file {bn(a)}", "value": "",
                                          "desc": f"Edit the file '{os.path.basename(str(a.get('path')))}'?"}))
    reg.register(Tool("list_dir", "List the files and folders in a folder with sizes and modified times. Use sort=\"size\" for the biggest first, "
                      "\"modified\" for the newest first, and limit to get only the top few.", list_dir,
                      {"path": "folder path", "sort": "name | size | modified", "limit": "how many items to return (max 200)"}, []))
    reg.register(Tool("make_dir", "Create a folder.", make_dir, {"path": "folder path"}, ["path"], risk="act",
                      approval=lambda a: {"domain": "files", "op": "mkdir", "label": f"create folder {bn(a)}", "value": "",
                                          "desc": f"Create the folder '{os.path.basename(str(a.get('path')))}'?"}))
    reg.register(Tool("move_file", "Move or rename a file/folder (refuses to overwrite).", move_file,
                      {"src": "from path", "dst": "to path"}, ["src", "dst"], risk="act",
                      approval=lambda a: {"domain": "files", "op": "move", "label": f"move file {bn(a, 'src')}", "value": "",
                                          "desc": f"Move '{os.path.basename(str(a.get('src')))}' to '{a.get('dst')}'?"}))
    reg.register(Tool("copy_file", "Copy a file (refuses to overwrite).", copy_file, {"src": "from path", "dst": "to path"},
                      ["src", "dst"], risk="act",
                      approval=lambda a: {"domain": "files", "op": "copy", "label": f"copy file {bn(a, 'src')}", "value": "",
                                          "desc": f"Copy '{os.path.basename(str(a.get('src')))}' to '{a.get('dst')}'?"}))
    reg.register(Tool("delete_file", "Delete a file or empty folder (it goes to a recoverable trash folder). DESTRUCTIVE.", delete_file,
                      {"path": "file path"}, ["path"], risk="irreversible",
                      approval=lambda a: {"domain": "files", "op": "delete", "label": f"delete file {bn(a)}", "value": "",
                                          "desc": f"Delete '{os.path.basename(str(a.get('path')))}'?", "force_ask": True}))

    def serve_folder(path, port=0):
        full = safe_path(path)
        if not os.path.isdir(full):
            raise NotADirectoryError(f"'{path}' is not a folder")

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        handler = functools.partial(Quiet, directory=full)
        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", int(port)), handler)   # loopback only
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        servers[url] = srv
        return {"url": url, "folder": full}

    def stop_server(url):
        srv = servers.pop(str(url), None)
        if not srv:
            raise KeyError("no such server")
        srv.shutdown()
        srv.server_close()
        return True

    def _cleanup():
        for u in list(servers):
            try:
                stop_server(u)
            except Exception:
                pass
    reg.cleanup = _cleanup
    reg.register(Tool("serve_folder", "Serve a folder on a local web address (this computer only) so a site can be tested in the browser.",
                      serve_folder, {"path": "folder to serve"}, ["path"], risk="act",
                      approval=lambda a: {"domain": "local", "op": "serve", "label": f"serve folder {bn(a)}", "value": "",
                                          "desc": f"Start a local test server for '{os.path.basename(str(a.get('path')))}' (this computer only)?"}))
    reg.register(Tool("stop_server", "Stop a local test server started with serve_folder.", stop_server, {"url": "its address"}, ["url"]))

    def _cmd_check(command, cwd=None, timeout=None):
        if not str(command or "").strip():
            return "empty command"
        if _SHELL_META.search(re.sub(r"\"[^\"]*\"|'[^']*'", "", str(command))):          # (no shell runs it: quoted ; | & are just text)
            return "no pipes, chaining, redirects or substitutions - run one plain command per step"
        try:
            parts = _split_command(str(command))
        except ValueError as e:
            return f"couldn't parse the command ({e})"
        if not parts:
            return "empty command"
        exe = _exe_name(parts[0])
        if exe in policy["deny"]:
            return f"'{exe}' is not something I'm allowed to run"
        if cwd:
            try:
                safe_path(cwd)
            except Exception as e:  # noqa: BLE001
                return str(e)
        return None

    def run_command(command, cwd=None, timeout=120):
        err = _cmd_check(command, cwd)
        if err:
            raise PermissionError(err)
        parts = _split_command(str(command))
        where = safe_path(cwd) if cwd else files_dir
        exe = shutil.which(parts[0])
        if not exe:
            if parts[0].lower() in ("dir", "copy", "echo", "type", "del", "cd", "md", "mkdir", "rd", "ren", "move", "cls", "set", "start"):
                raise FileNotFoundError(f"'{parts[0]}' is a Windows shell built-in, not a program, so I can't run it - use list_dir / read_file / "
                                        f"copy_file / move_file instead")
            raise FileNotFoundError(f"'{parts[0]}' isn't installed or isn't on the PATH")
        t = min(max(float(timeout or 120), 1), 1800)
        try:
            p = subprocess.run([exe] + parts[1:], cwd=where, capture_output=True, text=True, timeout=t,
                               stdin=subprocess.DEVNULL, errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timed out after {int(t)}s", "data": {"returncode": None}}
        out, errtxt = redact(p.stdout or "")[-3000:], redact(p.stderr or "")[-3000:]
        combined = (out + "\n" + errtxt).strip()
        data = {"returncode": p.returncode, "stdout": out, "stderr": errtxt, "text": combined[-1200:]}
        tb = re.search(r"Traceback \(most recent call last\):.*", combined, re.S)   # Blender exits 0 even when a -P script crashes
        if p.returncode != 0 or tb:
            return {"ok": False, "error": (tb.group(0) if tb else f"exit code {p.returncode}: {combined}")[-500:], "data": data}
        return {"ok": True, "data": data}

    def _cmd_approval(a):
        parts = _split_command(str(a.get("command", "")))
        exe, folder = _exe_name(parts[0]) if parts else "?", os.path.basename(os.path.abspath(a.get("cwd") or files_dir))
        if exe in policy["tools"]:
            return {"domain": folder, "op": "run", "label": f"run {exe}", "value": "",
                    "desc": f"Run '{short(a.get('command'), 90)}' in the {folder} folder?"}
        return {"domain": folder, "op": "run", "label": f"run {short(a.get('command'), 60)}", "value": "",
                "force_ask": bool(re.search(r"\b(del|erase|rd|rmdir|rm|format|uninstall|remove|shutdown|restart|reg\s+delete|wipe)\b", str(a.get("command", "")), re.I)),
                "desc": f"Run this command in the {folder} folder: {short(a.get('command'), 120)}?"}
    reg.register(Tool("run_command", "Run ONE program with arguments (no pipes/chaining), e.g. 'git status' or 'netlify deploy --dir=site'. "
                      "Returns its output. Can run any program on the PATH, including package managers such as winget or pip "
                      "(only destructive commands ask first). Dangerous system commands are refused.", run_command,
                      {"command": "the command line", "cwd": "folder to run in", "timeout": "seconds (default 120, max 1800 - installers can take many minutes, so set it)"}, ["command"],
                      risk="act", approval=_cmd_approval, precheck=lambda a: _cmd_check(a.get("command"), a.get("cwd"))))

    if search_fn:
        def _web_search(query):
            text = str(search_fn(query))
            urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"')\]]+", text)))
            urls = [u.rstrip(".,;:!?") for u in urls]
            return {"text": text, "urls": urls[:8], "first_url": urls[0] if urls else ""}
        reg.register(Tool("web_search", "Search the web: returns a written summary (text) plus any addresses it mentioned (urls, first_url). "
                          "It does NOT open a page - to use a site like a person, use a screen step.",
                          _web_search, {"query": "what to search for"}, ["query"]))
    if open_app_fn:
        reg.register(Tool("open_app", "Open a desktop application by name.",
                          lambda name: open_app_fn(name), {"name": "application name"}, ["name"], risk="act",
                          approval=lambda a: {"domain": "apps", "op": "open", "label": f"open {short(a.get('name'))}",
                                              "value": "", "desc": f"Open {a.get('name')}?"}))
    if notify_fn:
        reg.register(Tool("notify", "Tell the user something (chat; set speak=true to also say it aloud).",
                          lambda text, speak=False: notify_fn(str(text), bool(speak)) or True,
                          {"text": "the message", "speak": "true to say it aloud"}, ["text"]))
    if mem_search:
        reg.register(Tool("recall", "Look things up in my memory of the user.",
                          lambda query: "\n".join(mem_search(query) or []), {"query": "what to remember"}, ["query"]))
    if mem_add:
        reg.register(Tool("remember", "Save a fact about the user to my memory.",
                          lambda text: mem_add(str(text), ["automation"]) or True, {"text": "the fact"}, ["text"],
                          risk="act", approval=lambda a: {"domain": "memory", "op": "remember", "label": "remember a fact",
                                                          "value": "", "desc": f"Remember: {short(a.get('text'), 80)}?"}))
    if watch_fn:
        reg.register(Tool("start_presence_watch", "Start watching a game window for a friend joining/leaving.",
                          lambda friend, app="minecraft": watch_fn({"target_app": app, "friend_name": friend}) or True,
                          {"friend": "the friend's in-game name", "app": "game/app name"}, ["friend"], risk="act",
                          approval=lambda a: {"domain": str(a.get("app", "minecraft")), "op": "start",
                                              "label": f"watch {a.get('friend')}", "value": str(a.get("friend")),
                                              "desc": f"{str(a.get('app', 'minecraft')).capitalize()} is open. Want me to watch for {a.get('friend')}?"}))
    reg.register(Tool("wait", "Pause for a number of seconds (max 120).",
                      lambda seconds: (time.sleep(min(max(float(seconds), 0), 120)) or True),
                      {"seconds": "how long"}, ["seconds"], recovery=True))

    if hands is not None:
        def fetch_page(url, max_chars=4000):
            r = hands.browser_open(url)
            if not r["ok"] or r.get("needs_human") and not (r.get("data") or {}).get("captcha"):
                return r
            o = hands.browser_read(None, int(max_chars))
            return o
        reg.register(Tool("fetch_page", "Open a web page in my browser and read its text.", fetch_page,
                          {"url": "http(s) address", "max_chars": "limit"}, ["url"]))
        reg.register(Tool("restart_browser", "Reopen my browser if it was closed or crashed.",
                          lambda: hands.browser_restart(), {}, [], risk="act", recovery=True))
        reg.register(Tool("desktop_windows", "List the open windows.", lambda: hands.desktop_windows(), {}, []))
        reg.register(Tool("desktop_read", "Read the text inside a window.", lambda window: hands.desktop_read(window),
                          {"window": "title or program name"}, ["window"]))
        reg.register(Tool("desktop_focus", "Bring a window to the front.", lambda window: hands.desktop_focus(window),
                          {"window": "title or program name"}, ["window"], risk="act", recovery=True,
                          approval=lambda a: {"domain": "desktop", "op": "focus", "label": f"focus {short(a.get('window'))}",
                                              "value": "", "desc": f"Bring '{a.get('window')}' to the front?"}))
        reg.register(Tool("desktop_hotkey", "Press a key combination in a window (e.g. ctrl+s).",
                          lambda window, keys: hands.desktop_hotkey(window, keys), {"window": "window", "keys": "e.g. ctrl+s"},
                          ["window", "keys"], risk="act",
                          approval=lambda a: {"domain": "desktop", "op": "hotkey", "label": f"press {short(a.get('keys'))} in {short(a.get('window'))}",
                                              "value": "", "desc": f"Press {a.get('keys')} in '{a.get('window')}'?"}))
    def _with_target(tool, key):
        orig = tool.approval

        def wrapped(a):
            d = orig(a)
            try:
                full = safe_path(a.get(key))
            except Exception:
                full = str(a.get(key) or "")
            d["target"] = full
            if d.get("op") in ("edit", "append", "move", "overwrite", "delete") and full and not inside(full):
                d["force_ask"] = True                               # outside the folders she works in: changing it needs your permission
                d["desc"] = "That file is outside the folders I work in. " + str(d.get("desc", ""))
            return d
        tool.approval = wrapped
    for _n in ("write_file", "append_file", "overwrite_file", "edit_file", "delete_file", "move_file"):
        if _n in reg._t and reg._t[_n].approval:
            _with_target(reg._t[_n], "src" if _n == "move_file" else "path")

    def _path_check(names):
        def check(a):
            for n in names:
                if a.get(n) not in (None, ""):
                    try:
                        safe_path(a.get(n))
                    except (PermissionError, ValueError) as e:
                        return f"{type(e).__name__}: {e}"
            return None
        return check
    for _n, _args in (("write_file", ("path",)), ("append_file", ("path",)), ("overwrite_file", ("path",)), ("edit_file", ("path",)),
                      ("make_dir", ("path",)), ("move_file", ("src", "dst")), ("copy_file", ("src", "dst")), ("delete_file", ("path",))):
        if _n in reg._t and not reg._t[_n].precheck:
            reg._t[_n].precheck = _path_check(_args)

    return reg
