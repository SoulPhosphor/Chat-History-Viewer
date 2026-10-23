"""Automatic database backups (AUDIT.md #21).

Copies history.db and userdata.db into a `backups/` folder next to them (the
app folder, for the real databases) with
SQLite's own backup method, which gives a consistent copy even while the app
is using the database. Taken on start-up and before imports, bulk changes,
restores and database changes. The newest KEEP copies of each database are
kept; older ones are deleted.
"""
from __future__ import annotations

import datetime
import re
import sqlite3
import threading
from pathlib import Path

KEEP = 5
_LOCK = threading.Lock()
_SAFE_REASON = re.compile(r"[^a-z0-9-]+")


def backup_file(db_path: Path, reason: str, backup_dir: Path | None = None) -> Path | None:
    """Copy one database. Returns the copy's path, or None if the database
    does not exist yet. Errors are raised so the caller can decide to stop."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    folder = Path(backup_dir) if backup_dir else db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    tag = _SAFE_REASON.sub("-", reason.lower()).strip("-") or "manual"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = folder / f"{db_path.stem}-{stamp}-{tag}.db"
    with _LOCK:
        src = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
        try:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        _prune(folder, db_path.stem)
    return dest


def _prune(folder: Path, stem: str) -> None:
    pattern = re.compile(re.escape(stem) + r"-\d{8}-\d{6}-\d{6}-[a-z0-9-]+\.db$")
    copies = sorted(p for p in folder.iterdir() if pattern.match(p.name))
    for old in copies[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def backup_all(db_path: Path, reason: str, backup_dir: Path | None = None) -> list[Path]:
    """Back up history.db and its sibling userdata.db."""
    db_path = Path(db_path)
    made = []
    for path in (db_path, db_path.parent / "userdata.db"):
        copy = backup_file(path, reason, backup_dir)
        if copy:
            made.append(copy)
    return made


def list_backups(folder: Path) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted(folder.glob("*.db"))
