"""Reusable logic for the manual safety snapshots.

A snapshot captures everything the user added (labels, folders, tags, mood
tags, titles, archive/delete state, pins, bookmarks, notes, summaries, Scratch
Pads, related links, model tags, message edits, hidden messages and sticky
notes) — never message content. They are created
only by an explicit button press (never automatically), capped at
``SNAPSHOT_MAX``, and deleted only by the user. Restore is transactional and
skips — rather than inventing — conversations a snapshot references that no
longer resolve. The payload is versioned so future categories can be added.

As with the other API-logic modules, these functions take an open connection
(user-data DB attached as ``udb``) and return JSON-able dicts; error cases raise
``ApiError``. The caller (server.py) owns opening/closing the connection.
"""
from __future__ import annotations
import datetime
import json
import sqlite3
import time
import uuid

from api_common import ApiError
from request_limits import SNAPSHOT_NAME_MAX, check_text

import tag_ids

# 2 added the TABLE_CATEGORIES below (AUDIT.md #19); 3 added message edits,
# hidden messages and sticky notes. An older snapshot still restores the
# categories it has.
SNAPSHOT_SCHEMA_VERSION = 3
READABLE_VERSIONS = (1, 2, 3)
SNAPSHOT_MAX = 10

# Categories kept as whole tables: key -> (udb table, its conversation
# columns). Restore empties the table and puts the recorded rows back, leaving
# out rows for conversations that no longer exist.
TABLE_CATEGORIES = {
    "mood_tags": ("conversation_mood_tags", ("conversation_id",)),
    "bookmarks": ("conversation_bookmarks", ("conversation_id",)),
    "notes": ("conversation_notes", ("conversation_id",)),
    "summaries": ("conversation_summaries", ("conversation_id",)),
    "links": ("conversation_links", ("conversation_a", "conversation_b")),
    "model_tags": ("conversation_models", ("conversation_id",)),
    "scratch_pads": ("scratch_pads", ()),
    "message_edits": ("message_edits", ("conversation_id",)),
    "hidden_messages": ("hidden_messages", ("conversation_id",)),
    "sticky_notes": ("sticky_notes", ("conversation_id",)),
}

# The restorable categories, in display order, with human-readable names.
SNAPSHOT_CATEGORIES = [
    ("labels", "Labels"),
    ("folders", "Folders"),
    ("tags", "Tags"),
    ("mood_tags", "Mood tags"),
    ("titles", "Titles"),
    ("meta_state", "Archive/Delete state"),
    ("pins", "Pins"),
    ("bookmarks", "Bookmarks"),
    ("notes", "Notes"),
    ("summaries", "Summaries"),
    ("links", "Related links"),
    ("model_tags", "Model tags"),
    ("scratch_pads", "Scratch Pads"),
    ("message_edits", "Message edits"),
    ("hidden_messages", "Hidden messages"),
    ("sticky_notes", "Sticky notes"),
]


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA udb.table_info({table})").fetchall()]


def _restore_table(conn, table, conv_cols, rows, valid):
    """Put a table back to the recorded rows. A row for a conversation that no
    longer exists is skipped and counted. Saved versions (Notes, Summaries,
    Scratch Pads) move past every earlier number, so an editor still open
    from before the restore can't save over it."""
    cols = _columns(conn, table)
    top = 0
    mark_key = f"snapshot_version_mark:{table}"
    if "version" in cols:
        # The highest version ever handed out, kept outside the rows so an
        # empty restore in between can't make a number come round again.
        top = conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM udb.{table}").fetchone()[0]
        mark = conn.execute("SELECT meta_value FROM udb.udb_meta WHERE meta_key = ?",
                            (mark_key,)).fetchone()
        top = max(top, int(mark[0]) if mark else 0)
    conn.execute(f"DELETE FROM udb.{table}")
    skipped = 0
    for row in rows or []:
        if any(row.get(c) not in valid for c in conv_cols):
            skipped += 1
            continue
        keys = [c for c in cols if c in row]
        if not keys:
            continue
        conn.execute(
            f"INSERT OR REPLACE INTO udb.{table} ({', '.join(keys)}) "
            f"VALUES ({', '.join('?' for _ in keys)})",
            [row[c] for c in keys],
        )
    if "version" in cols:
        conn.execute(f"UPDATE udb.{table} SET version = COALESCE(version, 0) + ?", (top + 1,))
        new_top = conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM udb.{table}").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO udb.udb_meta (meta_key, meta_value) VALUES (?, ?)",
                     (mark_key, str(max(new_top, top + 1))))
    return skipped


def capture_snapshot(conn):
    """Gather the mutable-metadata payload and a per-category count map."""
    def rows(sql):
        return [dict(r) for r in conn.execute(sql).fetchall()]
    label_defs = rows("SELECT id, name, color, sort_index, created_at, updated_at FROM udb.labels")
    label_asg = rows("SELECT conversation_id, label_id, assigned_at FROM udb.conversation_labels")
    folder_defs = rows("SELECT id, name, created_at, updated_at, provider FROM udb.folders")
    folder_mem = rows("SELECT conversation_id, folder_id, added_at, pinned FROM udb.folder_items")
    tags = rows("SELECT conversation_id, tag, added_at FROM udb.conversation_tags")
    titles = rows("SELECT conversation_id, custom_title FROM udb.conversation_meta "
                  "WHERE custom_title IS NOT NULL AND custom_title <> ''")
    meta_state = rows("SELECT conversation_id, archived, deleted FROM udb.conversation_meta "
                      "WHERE COALESCE(archived,0)=1 OR COALESCE(deleted,0)=1")
    pins = rows("SELECT conversation_id, pinned_at, order_index FROM udb.pinned_conversations")
    tables = {key: rows(f"SELECT * FROM udb.{table}")
              for key, (table, _) in TABLE_CATEGORIES.items()}
    data = {
        **tables,
        "labels": {"definitions": label_defs, "assignments": label_asg},
        "folders": {"definitions": folder_defs, "memberships": folder_mem},
        "tags": tags,
        "titles": titles,
        "meta_state": meta_state,
        "pins": pins,
    }
    counts = {
        "labels": len(label_defs), "label_assignments": len(label_asg),
        "folders": len(folder_defs), "folder_items": len(folder_mem),
        "tags": len(tags), "titles": len(titles),
        "meta_state": len(meta_state), "pins": len(pins),
        **{key: len(value) for key, value in tables.items()},
    }
    return data, counts

def _snapshot_rows(conn):
    rows = conn.execute(
        "SELECT id, name, created_at, schema_version, item_count, payload_size, summary "
        "FROM udb.snapshots ORDER BY created_at DESC, id DESC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            summary = json.loads(r["summary"] or "{}")
        except Exception:
            summary = {}
        out.append({
            "id": r["id"], "name": r["name"], "created_at": r["created_at"],
            "schema_version": r["schema_version"], "item_count": r["item_count"],
            "payload_size": r["payload_size"], "summary": summary,
        })
    return out

def restore_categories(conn, data, selected):
    """Return each selected category to its recorded state. A conversation
    the snapshot references that no longer exists is skipped (counted), never
    recreated. Caller owns the transaction."""
    valid = {r[0] for r in conn.execute("SELECT id FROM conversations").fetchall()}
    skipped = 0

    if "labels" in selected:
        conn.execute("DELETE FROM udb.conversation_labels")
        conn.execute("DELETE FROM udb.labels")
        def_ids = set()
        for d in (data.get("labels") or {}).get("definitions") or []:
            conn.execute(
                "INSERT INTO udb.labels(id, name, color, sort_index, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (d.get("id"), d.get("name"), d.get("color"), d.get("sort_index") or 0,
                 d.get("created_at"), d.get("updated_at")),
            )
            def_ids.add(d.get("id"))
        for a in (data.get("labels") or {}).get("assignments") or []:
            if a.get("conversation_id") not in valid or a.get("label_id") not in def_ids:
                skipped += 1; continue
            conn.execute(
                "INSERT OR REPLACE INTO udb.conversation_labels(conversation_id, label_id, assigned_at) "
                "VALUES (?,?,?)",
                (a["conversation_id"], a["label_id"], a.get("assigned_at")),
            )

    if "folders" in selected:
        conn.execute("DELETE FROM udb.folder_items")
        conn.execute("DELETE FROM udb.folders")
        fids = set()
        for f in (data.get("folders") or {}).get("definitions") or []:
            conn.execute(
                "INSERT INTO udb.folders(id, name, created_at, updated_at, provider) "
                "VALUES (?,?,?,?,?)",
                (f.get("id"), f.get("name"), f.get("created_at"), f.get("updated_at"),
                 f.get("provider")),
            )
            fids.add(f.get("id"))
        for m in (data.get("folders") or {}).get("memberships") or []:
            if m.get("conversation_id") not in valid or m.get("folder_id") not in fids:
                skipped += 1; continue
            conn.execute(
                "INSERT OR REPLACE INTO udb.folder_items(conversation_id, folder_id, added_at, pinned) "
                "VALUES (?,?,?,?)",
                (m["conversation_id"], m["folder_id"], m.get("added_at"), m.get("pinned") or 0),
            )

    if "tags" in selected:
        conn.execute("DELETE FROM udb.conversation_tags")
        for t in data.get("tags") or []:
            if t.get("conversation_id") not in valid:
                skipped += 1; continue
            conn.execute(
                "INSERT OR REPLACE INTO udb.conversation_tags(conversation_id, tag, added_at) "
                "VALUES (?,?,?)",
                (t["conversation_id"], t["tag"], t.get("added_at")),
            )
        tag_ids.fill_missing(conn)

    for key, (table, conv_cols) in TABLE_CATEGORIES.items():
        if key in selected:
            skipped += _restore_table(conn, table, conv_cols, data.get(key), valid)
    if "mood_tags" in selected:
        tag_ids.fill_missing(conn)

    if "titles" in selected:
        # Reset every custom title, then reapply the recorded ones — so titles
        # set after the snapshot are reverted too. Touches only custom_title.
        conn.execute("UPDATE udb.conversation_meta SET custom_title = NULL")
        for ti in data.get("titles") or []:
            cid = ti.get("conversation_id")
            if cid not in valid:
                skipped += 1; continue
            conn.execute(
                "INSERT INTO udb.conversation_meta(conversation_id, custom_title, archived, deleted) "
                "VALUES (?,?,0,0) ON CONFLICT(conversation_id) DO UPDATE SET "
                "custom_title = excluded.custom_title",
                (cid, ti.get("custom_title")),
            )

    if "meta_state" in selected:
        # Reset archive/delete everywhere, then reapply recorded flags. Touches
        # only archived/deleted, never custom_title.
        conn.execute("UPDATE udb.conversation_meta SET archived = 0, deleted = 0")
        for ms in data.get("meta_state") or []:
            cid = ms.get("conversation_id")
            if cid not in valid:
                skipped += 1; continue
            conn.execute(
                "INSERT INTO udb.conversation_meta(conversation_id, custom_title, archived, deleted) "
                "VALUES (?, NULL, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
                "archived = excluded.archived, deleted = excluded.deleted",
                (cid, int(bool(ms.get("archived"))), int(bool(ms.get("deleted")))),
            )

    if "pins" in selected:
        conn.execute("DELETE FROM udb.pinned_conversations")
        for p in data.get("pins") or []:
            cid = p.get("conversation_id")
            if cid not in valid:
                skipped += 1; continue
            conn.execute(
                "INSERT OR REPLACE INTO udb.pinned_conversations(conversation_id, pinned_at, order_index) "
                "VALUES (?,?,?)",
                (cid, p.get("pinned_at"), p.get("order_index")),
            )

    return skipped


def snapshot_list(conn):
    return {"snapshots": _snapshot_rows(conn), "max": SNAPSHOT_MAX}


def snapshot_create(conn, payload_in):
    name = check_text("Snapshot name", str(payload_in.get("name") or "").strip(), SNAPSHOT_NAME_MAX)
    count = conn.execute("SELECT COUNT(*) FROM udb.snapshots").fetchone()[0]
    if count >= SNAPSHOT_MAX:
        # Never auto-delete the oldest; the user must make room.
        raise ApiError(
            f"Maximum of {SNAPSHOT_MAX} snapshots reached. "
            "Delete a snapshot before creating another.", 409)
    data, counts = capture_snapshot(conn)
    blob = json.dumps(
        {"version": SNAPSHOT_SCHEMA_VERSION, "categories": counts, "data": data},
        ensure_ascii=False,
    )
    if not name:
        name = "Snapshot " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    sid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO udb.snapshots(id, name, created_at, schema_version, payload, "
        "item_count, payload_size, summary) VALUES (?,?,?,?,?,?,?,?)",
        (sid, name, time.time(), SNAPSHOT_SCHEMA_VERSION, blob,
         sum(counts.values()), len(blob.encode("utf-8")),
         json.dumps(counts, ensure_ascii=False)),
    )
    conn.commit()
    return {"snapshots": _snapshot_rows(conn), "max": SNAPSHOT_MAX}


def snapshot_delete(conn, sid):
    sid = (sid or "").strip()
    conn.execute("DELETE FROM udb.snapshots WHERE id = ?", (sid,))
    conn.commit()
    return {"snapshots": _snapshot_rows(conn), "max": SNAPSHOT_MAX}


def snapshot_restore(conn, sid, body):
    sid = (sid or "").strip()
    valid_cats = {k for k, _ in SNAPSHOT_CATEGORIES}
    req = body.get("categories")
    if isinstance(req, list):
        selected = [c for c in req if c in valid_cats]
    else:
        selected = list(valid_cats)  # default: all supported categories
    if not selected:
        raise ApiError("Select at least one category to restore.", 400)
    row = conn.execute(
        "SELECT payload, schema_version FROM udb.snapshots WHERE id = ?", (sid,)
    ).fetchone()
    if not row:
        raise ApiError("Unknown snapshot", 404)
    try:
        parsed = json.loads(row["payload"] or "{}")
    except Exception:
        raise ApiError("Snapshot payload is corrupt", 400)
    if int(parsed.get("version") or row["schema_version"] or 0) not in READABLE_VERSIONS:
        raise ApiError("This snapshot was made by a newer version of the app.", 400)
    data = parsed.get("data") or {}
    # An older snapshot doesn't hold the newer categories: restoring one of
    # those would empty it, so it is left as it is.
    selected = [c for c in selected if c in data]
    if not selected:
        raise ApiError("This snapshot doesn't hold the categories you chose.", 400)
    try:
        skipped = restore_categories(conn, data, selected)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise ApiError("The restore failed and was rolled back", 500)
    return {"ok": True, "skipped": skipped, "restored": selected}
