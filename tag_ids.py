"""Stable ids for tags and mood tags (AUDIT.md #11).

A tag is one row in udb.tags: (id, kind, name). `kind` is 'tag' for ordinary
tags and 'mood' for mood tags, which stay a separate set. A conversation's
rows in conversation_tags / conversation_mood_tags keep the tag text (every
search and list reads it) and point at the tag's id in `tag_id`, so a later
rename or the API can refer to a tag by an id that never changes.
"""
from __future__ import annotations

import time
import uuid

# kind -> the table holding which conversations carry tags of that kind.
TABLES = {"tag": "conversation_tags", "mood": "conversation_mood_tags"}


def tag_id(conn, kind: str, name: str, schema: str = "udb") -> str:
    """The id of the tag `name` of this kind, made the first time it is used."""
    row = conn.execute(
        f"SELECT id FROM {schema}.tags WHERE kind = ? AND name = ?", (kind, name)
    ).fetchone()
    if row:
        return row[0]
    new_id = uuid.uuid4().hex
    conn.execute(
        f"INSERT INTO {schema}.tags (id, kind, name, created_at) VALUES (?, ?, ?, ?)",
        (new_id, kind, name, time.time()),
    )
    return new_id


def fill_missing(conn, schema: str = "udb") -> None:
    """Give every conversation tag row without an id the id of its tag. Used
    by the database change that added ids and after a snapshot restore."""
    for kind, table in TABLES.items():
        names = [r[0] for r in conn.execute(
            f"SELECT DISTINCT tag FROM {schema}.{table} WHERE tag_id IS NULL"
        ).fetchall()]
        for name in names:
            conn.execute(
                f"UPDATE {schema}.{table} SET tag_id = ? WHERE tag = ? AND tag_id IS NULL",
                (tag_id(conn, kind, name, schema), name),
            )
