"""Every table that holds rows belonging to one conversation (AUDIT.md #7).

Permanent delete (server._purge_conversation) and the rebuild's "stay
deleted" pass (server.rebuild_history) both use these lists, so a table added later — tag ids, note
versions, anything the AI writes — is covered by adding it here. Each entry is
(table, conversation column, provider column or None). A provider column means
only the row with the conversation's own side is removed: the same id on the
other side is a different conversation.

`conversations` itself is removed last, by delete_conversation_rows.
"""
from __future__ import annotations

import json

# history.db: what an import writes for the conversation...
IMPORTED_TABLES = (
    ("messages", "conversation_id", None),
    ("search_index", "conversation_id", None),
    ("artifacts", "conv_id", None),
    # The import audit's copy of the title and preview.
    ("import_sources", "conversation_id", None),
)
# ...and the rest of history.db (the Compare bar and the review list moved to
# userdata.db in history change 3).
HISTORY_TABLES = IMPORTED_TABLES

# userdata.db (attached as `udb`): what the user added to the conversation.
USERDATA_TABLES = (
    ("conversation_meta", "conversation_id", None),
    ("pinned_conversations", "conversation_id", None),
    ("folder_items", "conversation_id", None),
    ("conversation_tags", "conversation_id", None),
    ("conversation_mood_tags", "conversation_id", None),
    ("conversation_models", "conversation_id", None),
    ("conversation_model_flags", "conversation_id", None),
    ("conversation_labels", "conversation_id", None),
    ("conversation_bookmarks", "conversation_id", None),
    ("message_edits", "conversation_id", None),
    ("hidden_messages", "conversation_id", None),
    ("sticky_notes", "conversation_id", None),
    ("conversation_notes", "conversation_id", None),
    ("conversation_summaries", "conversation_id", None),
    ("conversation_links", "conversation_a", None),
    ("conversation_links", "conversation_b", None),
    ("workspace_tabs", "conversation_id", None),
    ("pending_reimport", "conversation_id", "provider"),
)


def _has_table(conn, schema: str, table: str) -> bool:
    try:
        return conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False  # the schema is not attached


def _delete_from(conn, schema: str, tables, cid: str, provider: str | None) -> None:
    for table, column, provider_column in tables:
        if not _has_table(conn, schema, table):
            continue
        sql = f"DELETE FROM {schema}.{table} WHERE {column} = ?"
        params = [cid]
        if provider_column:
            sql += f" AND {provider_column} = ?"
            params.append(provider or "")
        conn.execute(sql, params)


def delete_conversation_rows(conn, cid: str, provider: str | None) -> None:
    """Remove every row that belongs to one conversation, from both databases,
    including its place in unfinished bulk-label runs. A table missing from an
    older or partial database is skipped; any other error is raised, so the
    caller's transaction rolls the whole delete back."""
    _delete_from(conn, "main", HISTORY_TABLES, cid, provider)
    _delete_from(conn, "udb", USERDATA_TABLES, cid, provider)
    if _has_table(conn, "udb", "label_bulk_runs"):
        _drop_from_bulk_runs(conn, cid)
    conn.execute("DELETE FROM main.conversations WHERE id = ?", (cid,))


def delete_imported_rows(conn, cid: str) -> None:
    """Remove only what an import wrote. A rebuild uses this for a chat that
    was permanently deleted: everything else about it is already gone, and a
    copy held aside for review (pending_reimport) must stay."""
    _delete_from(conn, "main", IMPORTED_TABLES, cid, None)
    conn.execute("DELETE FROM main.conversations WHERE id = ?", (cid,))


def _drop_from_bulk_runs(conn, cid: str) -> None:
    """An unfinished bulk-label run keeps its frozen list of conversation ids.
    Take this one out and keep the counts in step, so Continue neither skips
    a conversation nor counts a deleted one."""
    rows = conn.execute(
        "SELECT id, ids_json, completed_count, total_qualifying FROM udb.label_bulk_runs "
        "WHERE ids_json LIKE ?",
        ("%" + json.dumps(cid) + "%",),
    ).fetchall()
    for run_id, ids_json, completed, total in rows:
        try:
            ids = json.loads(ids_json or "[]")
        except ValueError:
            continue
        if cid not in ids:
            continue
        completed = int(completed or 0)
        total = int(total or len(ids))
        done_before = ids[:completed].count(cid)
        kept = [i for i in ids if i != cid]
        removed = len(ids) - len(kept)
        conn.execute(
            "UPDATE udb.label_bulk_runs SET ids_json = ?, completed_count = ?, "
            "total_qualifying = ? WHERE id = ?",
            (json.dumps(kept), completed - done_before, max(0, total - removed), run_id),
        )
