"""Numbered database changes (AUDIT.md #18).

Each database (history.db and userdata.db) has a `schema_version` table holding
one number: how many of its change steps have been applied. A change step is a
function that takes an open connection. Steps run in order, once each, and the
version is raised after every step that succeeds.

A step that fails raises SchemaChangeError naming the database and the step,
instead of being hidden. "Column already exists" is not a failure: use
add_column(), which checks first.

Adding a change: append a new function to the database's list in server.py.
Never edit or reorder a step that has already shipped.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, Sequence

Step = Callable[[sqlite3.Connection], None]


class SchemaChangeError(Exception):
    """A numbered database change failed."""


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " version INTEGER NOT NULL,"
        " updated_at REAL)"
    )


def get_version(conn: sqlite3.Connection, schema: str = "main") -> int:
    """The database's change number, or 0 if it has none yet."""
    try:
        row = conn.execute(f"SELECT version FROM {schema}.schema_version WHERE id = 1").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (id, version, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version, updated_at = excluded.updated_at",
        (version, time.time()),
    )


def pending_steps(conn: sqlite3.Connection, steps: Sequence[Step]) -> int:
    return max(0, len(steps) - get_version(conn))


def apply_steps(conn: sqlite3.Connection, db_name: str, steps: Sequence[Step]) -> int:
    """Run every step after the database's current version. Returns the new
    version. Raises SchemaChangeError on the first step that fails; the steps
    before it stay applied and recorded."""
    _ensure_version_table(conn)
    conn.commit()
    current = get_version(conn)
    if current > len(steps):
        raise SchemaChangeError(
            f"{db_name} is at change {current}, newer than this app knows "
            f"({len(steps)}). Update the app before opening this database."
        )
    for number in range(current + 1, len(steps) + 1):
        step = steps[number - 1]
        try:
            step(conn)
            _set_version(conn, number)
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise SchemaChangeError(
                f"{db_name} change {number} ({step.__name__}) failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return len(steps)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def add_column(conn: sqlite3.Connection, table: str, column_sql: str) -> bool:
    """Add a column unless it is already there. A missing table is skipped
    (returns False); any other error is raised, not hidden."""
    if not table_exists(conn, table):
        return False
    name = column_sql.split()[0]
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
    return True
