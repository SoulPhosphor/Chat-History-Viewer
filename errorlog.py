"""The error log (AUDIT.md #22).

Every error goes to `logs/errors.json` in the app folder, with enough detail to
fix it without asking anyone to reproduce it.

Privacy rule: the log never holds chat text, note or summary text, search words
or an API key. It holds only IDs, sizes, names, code locations and versions.
Request bodies are reduced to field names and sizes; quoted text in error
messages is replaced by its length; query-string values are never kept.

Repeats are grouped: the same place (server/browser), error type, line of code
and action add to one group, which keeps a count, the first and last time, and
the full details of its 5 most recent occurrences. The file keeps at most 200
groups; when full, the group whose last occurrence is oldest is dropped.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import threading
import traceback
import uuid
from pathlib import Path

import app_paths
from version import __version__

MAX_GROUPS = 200
RECENT_PER_GROUP = 5

LOG_DIR = app_paths.LOG_DIR
LOG_FILE = LOG_DIR / "errors.json"
DB_PATH = app_paths.DB_PATH
_LOCK = threading.Lock()


def configure(log_dir: Path | None = None, db_path: Path | None = None) -> None:
    """Point the log at another folder or database (tests use throwaway ones)."""
    global LOG_DIR, LOG_FILE, DB_PATH
    if log_dir is not None:
        LOG_DIR = Path(log_dir)
        LOG_FILE = LOG_DIR / "errors.json"
    if db_path is not None:
        DB_PATH = Path(db_path)

_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{12,64}|\d+|[A-Za-z]+[-_][A-Za-z0-9_-]{6,80})$"
)
_WORD_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_QUOTED_RE = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
_SAFE_QUOTED_RE = re.compile(r"^[\w.\-/<>:]{0,64}$")
_NUMBER_RE = re.compile(r"\d+")


# ---------------------------------------------------------------- privacy ---

def looks_like_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def scrub_message(text: str) -> str:
    """Keep the shape of an error message, drop any text it quotes. A quoted
    single word or name (a column, a file name, an ID) is kept; anything with
    spaces or longer than 64 characters becomes `<text: N chars>`."""
    def repl(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if _SAFE_QUOTED_RE.match(inner):
            return m.group(0)
        return f"<text: {len(inner)} chars>"
    text = _QUOTED_RE.sub(repl, str(text))
    if len(text) > 300:
        text = text[:300] + f"… <{len(text) - 300} more chars>"
    return text


def describe_size(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"{len(value)} chars"
    if isinstance(value, (list, tuple)):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} keys"
    return type(value).__name__


def request_shape(payload) -> dict:
    """Field names and their sizes, e.g. {"title": "42 chars"}. Nested objects
    are described one level down. Values themselves are never kept."""
    if not isinstance(payload, dict):
        return {"(body)": describe_size(payload)} if payload not in (None, {}) else {}
    shape = {}
    for key, value in list(payload.items())[:50]:
        key = str(key)[:64]
        if isinstance(value, dict):
            shape[key] = {str(k)[:64]: describe_size(v) for k, v in list(value.items())[:50]}
        else:
            shape[key] = describe_size(value)
    return shape


def ids_in_payload(payload, limit: int = 20) -> dict:
    """Values of fields named `id`, `*_id` or `*_ids` that look like IDs."""
    found: dict[str, list] = {}

    def visit(obj, depth):
        if depth > 2 or not isinstance(obj, dict):
            return
        for key, value in obj.items():
            k = str(key)
            if k.lower() == "id" or k.endswith(("_id", "Id")):
                vals = value if isinstance(value, list) else [value]
            elif k.lower() == "ids" or k.endswith(("_ids", "Ids")):
                vals = value if isinstance(value, list) else []
            else:
                if isinstance(value, dict):
                    visit(value, depth + 1)
                continue
            for v in vals[:limit]:
                if isinstance(v, int) and not isinstance(v, bool):
                    v = str(v)
                if looks_like_id(v):
                    found.setdefault(str(key)[:64], []).append(v)
    visit(payload, 0)
    return found


def describe_path(method: str, raw_path: str) -> tuple[str, list[str]]:
    """Turn a request path into an action with every value replaced, e.g.
    `PATCH /api/conversation/<id>`. Returns the action and the IDs found.
    The query string is reduced to its parameter names."""
    from urllib.parse import urlsplit, unquote, parse_qsl
    parts = urlsplit(raw_path or "")
    ids: list[str] = []
    segs = []
    for seg in parts.path.split("/"):
        if not seg:
            continue
        text = unquote(seg)
        if _WORD_RE.match(text) and not looks_like_id(text):
            segs.append(text)
        elif looks_like_id(text):
            segs.append("<id>")
            ids.append(text)
        else:
            segs.append(f"<value: {len(text)} chars>")
    action = f"{method} /" + "/".join(segs)
    names = sorted({k[:64] for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    if names:
        action += "?" + "&".join(f"{n}=…" for n in names)
    return action, ids


# ---------------------------------------------------------------- context ---

def _local_now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _db_versions() -> dict:
    import schema_changes
    out = {}
    for name in ("history.db", "userdata.db"):
        path = DB_PATH if name == "history.db" else DB_PATH.parent / name
        if not path.exists():
            out[name] = "missing"
            continue
        try:
            conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
            try:
                out[name] = schema_changes.get_version(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            out[name] = f"unreadable ({type(exc).__name__})"
    return out


def system_info() -> dict:
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "os": platform.platform(),
    }


def _frames_from_tb(tb) -> list[dict]:
    return [
        {
            "file": _short_file(f.filename),
            "line": f.lineno,
            "function": f.name,
            "code": (f.line or "").strip()[:200],
        }
        for f in traceback.extract_tb(tb)
    ]


def _short_file(name: str) -> str:
    try:
        return str(Path(name).resolve().relative_to(app_paths.APP_DIR))
    except (ValueError, OSError):
        return "<python>/" + Path(name).name


def _innermost_app_frame(frames: list[dict]) -> str:
    for f in reversed(frames):
        if not f["file"].startswith("<"):
            return f"{f['file']}:{f['line']}"
    return f"{frames[-1]['file']}:{frames[-1]['line']}" if frames else "?"


def _group_key(where: str, error_type: str, location: str, action: str) -> str:
    action = _NUMBER_RE.sub("#", action or "")
    raw = "\x1f".join((where, error_type, location, action))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- writing ---

def _load() -> dict:
    try:
        data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"format": 1, "groups": []}


def _save(data: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LOG_FILE)


def _add(entry: dict, key: str) -> dict:
    with _LOCK:
        data = _load()
        groups = data["groups"]
        group = next((g for g in groups if g.get("key") == key), None)
        if group is None:
            group = {
                "key": key,
                "where": entry["where"],
                "error": entry["error_type"],
                "location": entry["location"],
                "action": entry["action"],
                "count": 0,
                "first_seen": entry["when"],
                "recent": [],
            }
            groups.append(group)
        group["count"] += 1
        group["last_seen"] = entry["when"]
        group["recent"] = (group["recent"] + [entry])[-RECENT_PER_GROUP:]
        if len(groups) > MAX_GROUPS:
            groups.sort(key=lambda g: g.get("last_seen") or "")
            del groups[: len(groups) - MAX_GROUPS]
        _save(data)
    return entry


def record(
    *,
    where: str,
    error_type: str,
    message: str,
    frames: list[dict] | None = None,
    stack: str | None = None,
    action: str = "",
    status=None,
    ids: dict | None = None,
    shape: dict | None = None,
    browser: dict | None = None,
) -> dict | None:
    """Write one error. Never raises: a broken log must not break the app."""
    try:
        frames = frames or []
        location = _innermost_app_frame(frames) if frames else (
            scrub_message((stack or "").strip().splitlines()[0])[:120] if stack else "?"
        )
        entry = {
            "error_id": str(uuid.uuid4()),
            "when": _local_now(),
            "where": where,
            "error_type": error_type,
            "message": scrub_message(message),
            "location": location,
            "traceback": frames,
            "action": action,
            "status": status,
            "ids": ids or {},
            "request_shape": shape or {},
            "app_version": __version__,
            "db_versions": _db_versions(),
            "system": system_info(),
        }
        if stack is not None:
            entry["stack"] = scrub_message(stack) if len(stack) <= 300 else _scrub_stack(stack)
        if where == "browser":
            entry["browser"] = {
                "name": str((browser or {}).get("name") or "")[:80],
                "version": str((browser or {}).get("version") or "")[:40],
                "window": str((browser or {}).get("window") or "")[:20],
                "screen": str((browser or {}).get("screen") or "")[:80],
            }
        return _add(entry, _group_key(where, error_type, location, action))
    except Exception:
        try:
            sys.stderr.write("[errorlog] could not write the error log\n")
        except Exception:
            pass
        return None


def _scrub_stack(stack: str) -> str:
    return "\n".join(scrub_message(line)[:300] for line in stack.splitlines()[:60])


def record_exception(
    exc: BaseException,
    *,
    where: str = "server",
    action: str = "",
    status=None,
    ids: dict | None = None,
    shape: dict | None = None,
) -> dict | None:
    """Write a Python exception with its full traceback (file, line, function
    and code line of every step; no local variables)."""
    return record(
        where=where,
        error_type=f"{type(exc).__module__}.{type(exc).__qualname__}"
        if type(exc).__module__ not in ("builtins", "__main__") else type(exc).__qualname__,
        message=str(exc),
        frames=_frames_from_tb(exc.__traceback__),
        action=action,
        status=status,
        ids=ids,
        shape=shape,
    )


def read_log() -> dict:
    with _LOCK:
        return _load()


def recent_groups(limit: int = 10) -> list[dict]:
    """The `limit` groups that happened most recently, newest first."""
    groups = read_log().get("groups", [])
    groups = sorted(groups, key=lambda g: g.get("last_seen") or "", reverse=True)
    return groups[:limit]


def clear() -> None:
    """Empty the whole log file, not only the groups on screen."""
    with _LOCK:
        _save({"format": 1, "groups": []})


# ---------------------------------------------------------- browser errors ---

# "at fn (http://host/static/app.js:12:5)" (Chrome, Edge) or
# "fn@http://host/static/app.js:12:5" (Firefox, Safari).
_BROWSER_FRAME_RE = re.compile(
    r"^\s*(?:at\s+(?:(?P<fn1>.*?)\s+\()?(?P<url1>[^()\s]+?):(?P<line1>\d+):\d+\)?"
    r"|(?P<fn2>[^@\s]*)@(?P<url2>\S+?):(?P<line2>\d+):\d+)\s*$"
)


def browser_frames(stack: str) -> list[dict]:
    """Turn a browser stack trace into frames shaped like the server's,
    outermost call first (so the last frame is where it failed). Only the
    path of each script is kept, never the host or a query string."""
    from urllib.parse import urlsplit
    frames = []
    for line in str(stack or "").splitlines()[:60]:
        m = _BROWSER_FRAME_RE.match(line)
        if not m:
            continue
        url = m.group("url1") or m.group("url2") or ""
        fn = m.group("fn1") if m.group("url1") else m.group("fn2")
        line_no = int(m.group("line1") or m.group("line2") or 0)
        path = urlsplit(url).path if "://" in url else url
        frames.append({
            "file": (path.lstrip("/") or "?")[:120],
            "line": line_no,
            "function": (fn or "<anonymous>").replace("async ", "")[:80],
            "code": "",
        })
    frames.reverse()
    return frames


def _text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def record_browser(payload: dict) -> dict | None:
    """Write an error the browser sent (AUDIT.md #35). The request a failed
    call made is reduced to its address pattern; nothing else from the page
    is trusted beyond short, scrubbed strings."""
    action = _text(payload.get("action"), 80) or "Browser error"
    ids: dict = {}
    request = payload.get("request")
    if isinstance(request, dict):
        method = _text(request.get("method"), 10).upper() or "GET"
        if not _WORD_RE.match(method.lower()):
            method = "?"
        pattern, path_ids = describe_path(method, _text(request.get("url"), 2000))
        action = f"{action} ({pattern})"
        if path_ids:
            ids["path"] = path_ids[:20]
    status = payload.get("status")
    if not (isinstance(status, int) and not isinstance(status, bool)):
        status = None
    stack = _text(payload.get("stack"), 20000)
    error_type = _text(payload.get("error_type"), 60) or "Error"
    if not re.match(r"^[A-Za-z][\w.]*$", error_type):
        error_type = "Error"
    browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
    return record(
        where="browser",
        error_type=error_type,
        message=_text(payload.get("message"), 2000),
        frames=browser_frames(stack),
        stack=stack or None,
        action=action,
        status=status,
        ids=ids,
        browser=browser,
    )
