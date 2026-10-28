"""Message versions, the user's edits, hidden versions and sticky notes.

A message slot (one row of `messages`) can hold several versions: the
original and any rerolls the export kept (`messages.siblings`). Every version
has a fixed id: the export's own id for it (`uuid` in each sibling, or the
row's `message_uuid` for a message with one version). When a reply shown
under another version of the user's message has its own export id
(`asst_uuid`), that reply is a version too.

What the user adds lives in userdata.db and is keyed only by these ids, so a
re-import never replaces or undoes it:

- message_edits: at most one edit per version, with its own fixed id.
- hidden_messages: which versions or edits are hidden (by their id).
- sticky_notes: one note per message slot, with its own fixed id that never
  changes when the title or text changes.

A version with no export id (an export without ids) falls back to a key made
from its position ("pos:<seq>:<index>").

Every function takes an open connection (from server.open_db, with udb
attached), returns a JSON-able dict, and raises ApiError for an error status.
"""
from __future__ import annotations

import json
import re
import time
import uuid

import message_ids
from api_common import ApiError
from request_limits import NOTE_MAX, check_text

EDIT_MAX = 2_000_000
NOTE_TITLE_MAX = 200
DEFAULT_NOTE_TITLE = "Untitled"


def create_tables(conn) -> None:
    """userdata change 5. Run with userdata.db as the main schema."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS message_edits (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            version_uuid    TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_at      REAL,
            updated_at      REAL,
            UNIQUE (conversation_id, version_uuid)
        );
        CREATE TABLE IF NOT EXISTS hidden_messages (
            conversation_id TEXT NOT NULL,
            item_id         TEXT NOT NULL,
            hidden_at       REAL,
            PRIMARY KEY (conversation_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS sticky_notes (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            message_uuid    TEXT,
            seq             INTEGER NOT NULL,
            title           TEXT NOT NULL,
            body            TEXT NOT NULL DEFAULT '',
            created_at      REAL,
            updated_at      REAL,
            version         INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sticky_notes_message
            ON sticky_notes (conversation_id, message_uuid) WHERE message_uuid IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sticky_notes_position
            ON sticky_notes (conversation_id, seq) WHERE message_uuid IS NULL;
        """
    )


# ── Versions ──────────────────────────────────────────────────────────────────


def _siblings(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) and len(value) > 1 else None


def row_version_ids(seq, message_uuid, siblings_raw, branch_index):
    """The ids of a row's versions, in order, and of the replies kept with
    the user's other versions: ([version ids], [reply ids or None])."""
    sibs = _siblings(siblings_raw)
    if not sibs:
        return [message_uuid or f"pos:{seq}:0"], [None]
    active = max(0, min(int(branch_index or len(sibs)) - 1, len(sibs) - 1))
    ids, replies = [], []
    for i, sib in enumerate(sibs):
        sib = sib if isinstance(sib, dict) else {}
        vid = sib.get("uuid") or (message_uuid if i == active else None)
        ids.append(vid or f"pos:{seq}:{i}")
        # The reply kept with the active version is the next row itself.
        if "asst_content" in sib and i != active:
            replies.append(sib.get("asst_uuid") or f"pos:{seq}:{i}:reply")
        else:
            replies.append(None)
    return ids, replies


def conversation_versions(conn, conversation_id: str) -> dict:
    """Every version id in a conversation -> where it is shown:
    {"seq", "index"} for a version of that row, or {"seq", "index", "reply":
    True} for the reply kept with version `index` of the user row `seq`."""
    out = {}
    for seq, message_uuid, siblings_raw, branch_index in conn.execute(
        "SELECT seq, message_uuid, siblings, branch_index FROM messages "
        "WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    ):
        ids, replies = row_version_ids(seq, message_uuid, siblings_raw, branch_index)
        for i, vid in enumerate(ids):
            out.setdefault(vid, {"seq": seq, "index": i})
        for i, rid in enumerate(replies):
            if rid:
                out.setdefault(rid, {"seq": seq, "index": i, "reply": True})
    return out


def locate_item(conn, conversation_id: str, item_id: str):
    """Where a version or an edit is now, or None when it is gone.
    {"seq", "index", "reply"?, "version_uuid", "edit_id"?}"""
    if not item_id:
        return None
    versions = conversation_versions(conn, conversation_id)
    if item_id in versions:
        return {**versions[item_id], "version_uuid": item_id}
    row = conn.execute(
        "SELECT version_uuid FROM udb.message_edits WHERE conversation_id = ? AND id = ?",
        (conversation_id, item_id),
    ).fetchone()
    if row and row[0] in versions:
        return {**versions[row[0]], "version_uuid": row[0], "edit_id": item_id}
    return None


def _require_conversation(conn, conversation_id: str) -> str:
    conversation_id = (conversation_id or "").strip() if isinstance(conversation_id, str) else ""
    if not conversation_id:
        raise ApiError("Missing conversation id.", 400)
    if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
        raise ApiError("Conversation not found.", 404)
    return conversation_id


# ── Edits ─────────────────────────────────────────────────────────────────────


def _edit_row(row) -> dict:
    return {
        "id": row["id"],
        "version_uuid": row["version_uuid"],
        "content": row["content"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_edit(conn, payload: dict) -> dict:
    """Save the one edit of a version. Saving again changes that same edit
    (its id never changes); the original is never touched."""
    conversation_id = _require_conversation(conn, payload.get("conversation_id"))
    version_uuid = payload.get("version_uuid")
    if not isinstance(version_uuid, str) or not version_uuid.strip():
        raise ApiError("Missing message version.", 400)
    if version_uuid not in conversation_versions(conn, conversation_id):
        raise ApiError("That message is no longer in the conversation.", 404)
    content = payload.get("content")
    if not isinstance(content, str):
        raise ApiError("The edited message must be text.", 400)
    check_text("content", content, EDIT_MAX)
    if not content.strip():
        raise ApiError("The edited message is empty.", 400)
    now = time.time()
    existing = conn.execute(
        "SELECT id FROM udb.message_edits WHERE conversation_id = ? AND version_uuid = ?",
        (conversation_id, version_uuid),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE udb.message_edits SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, existing["id"]),
        )
        edit_id = existing["id"]
    else:
        edit_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO udb.message_edits (id, conversation_id, version_uuid, content, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (edit_id, conversation_id, version_uuid, content, now, now),
        )
    row = conn.execute("SELECT * FROM udb.message_edits WHERE id = ?", (edit_id,)).fetchone()
    conn.commit()
    return {"edit": _edit_row(row)}


def conversation_edits(conn, conversation_id: str) -> list:
    return [
        _edit_row(r) for r in conn.execute(
            "SELECT * FROM udb.message_edits WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
    ]


# ── Hidden versions ───────────────────────────────────────────────────────────


def set_hidden(conn, payload: dict) -> dict:
    """Hide or restore one version or edit. Only that one item changes."""
    conversation_id = _require_conversation(conn, payload.get("conversation_id"))
    item_id = payload.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ApiError("Missing message version.", 400)
    hidden = payload.get("hidden")
    if not isinstance(hidden, bool):
        raise ApiError("hidden must be true or false.", 400)
    if hidden:
        if locate_item(conn, conversation_id, item_id) is None:
            raise ApiError("That message is no longer in the conversation.", 404)
        conn.execute(
            "INSERT OR IGNORE INTO udb.hidden_messages (conversation_id, item_id, hidden_at) VALUES (?, ?, ?)",
            (conversation_id, item_id, time.time()),
        )
    else:
        conn.execute(
            "DELETE FROM udb.hidden_messages WHERE conversation_id = ? AND item_id = ?",
            (conversation_id, item_id),
        )
    conn.commit()
    return {"ok": True, "item_id": item_id, "hidden": hidden}


def conversation_hidden(conn, conversation_id: str) -> list:
    """Ids of the hidden items still in the conversation."""
    rows = [r[0] for r in conn.execute(
        "SELECT item_id FROM udb.hidden_messages WHERE conversation_id = ? ORDER BY hidden_at",
        (conversation_id,),
    )]
    if not rows:
        return []
    versions = conversation_versions(conn, conversation_id)
    edits = {r[0]: r[1] for r in conn.execute(
        "SELECT id, version_uuid FROM udb.message_edits WHERE conversation_id = ?",
        (conversation_id,),
    )}
    return [i for i in rows if i in versions or edits.get(i) in versions]


# ── Sticky notes ──────────────────────────────────────────────────────────────


def _note_row(row, seq=None) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "message_uuid": row["message_uuid"],
        "seq": row["seq"] if seq is None else seq,
        "title": row["title"],
        "body": row["body"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": row["version"],
    }


_UNTITLED = re.compile(r"^Untitled(?: (\d+))?$")


def next_untitled_title(conn, conversation_id: str) -> str:
    """"Untitled", then "Untitled 1", "Untitled 2"… within the conversation."""
    used = set()
    for (title,) in conn.execute(
        "SELECT title FROM udb.sticky_notes WHERE conversation_id = ?", (conversation_id,)
    ):
        match = _UNTITLED.match((title or "").strip())
        if match:
            used.add(int(match.group(1) or 0))
    if 0 not in used:
        return DEFAULT_NOTE_TITLE
    n = 1
    while n in used:
        n += 1
    return f"{DEFAULT_NOTE_TITLE} {n}"


def conversation_notes(conn, conversation_id: str) -> list:
    """The conversation's notes whose message is still there, placed at its
    current position."""
    out = []
    for row in conn.execute(
        "SELECT * FROM udb.sticky_notes WHERE conversation_id = ?", (conversation_id,)
    ).fetchall():
        seq = message_ids.locate(conn, conversation_id, row["message_uuid"], row["seq"])
        if seq is None:
            continue
        if seq != row["seq"]:
            conn.execute("UPDATE udb.sticky_notes SET seq = ? WHERE id = ?", (seq, row["id"]))
            conn.commit()
        out.append(_note_row(row, seq))
    out.sort(key=lambda n: n["seq"])
    return out


def _get_note(conn, note_id: str):
    row = conn.execute("SELECT * FROM udb.sticky_notes WHERE id = ?", (note_id,)).fetchone()
    if not row:
        raise ApiError("Sticky note not found.", 404)
    return row


def _clean_title(value) -> str:
    if not isinstance(value, str):
        raise ApiError("The title must be text.", 400)
    title = " ".join(value.split())
    if not title:
        raise ApiError("The title can't be empty.", 400)
    check_text("title", title, NOTE_TITLE_MAX)
    return title


def create_note(conn, payload: dict) -> dict:
    """The message's sticky note: made with the next free "Untitled" title,
    or the one it already has (a message has one note)."""
    conversation_id = _require_conversation(conn, payload.get("conversation_id"))
    try:
        seq = int(payload.get("seq"))
    except (TypeError, ValueError):
        raise ApiError("Invalid message number.", 400)
    row = conn.execute(
        "SELECT message_uuid FROM messages WHERE conversation_id = ? AND seq = ?",
        (conversation_id, seq),
    ).fetchone()
    if not row:
        raise ApiError("That message is no longer in the conversation.", 404)
    message_uuid = row[0] or None
    existing = conn.execute(
        "SELECT * FROM udb.sticky_notes WHERE conversation_id = ? AND "
        + ("message_uuid = ?" if message_uuid else "message_uuid IS NULL AND seq = ?"),
        (conversation_id, message_uuid or seq),
    ).fetchone()
    if existing:
        return {"note": _note_row(existing, seq), "created": False}
    now = time.time()
    note_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO udb.sticky_notes (id, conversation_id, message_uuid, seq, title, body, created_at, updated_at, version) "
        "VALUES (?, ?, ?, ?, ?, '', ?, ?, 0)",
        (note_id, conversation_id, message_uuid, seq, next_untitled_title(conn, conversation_id), now, now),
    )
    note = _note_row(_get_note(conn, note_id))
    conn.commit()
    return {"note": note, "created": True}


def update_note(conn, note_id: str, payload: dict) -> dict:
    """Change the title and/or text. The id never changes. A text save made
    from an older copy (base_version) is refused instead of overwriting."""
    _get_note(conn, note_id)
    sets, params = [], []
    if "title" in payload:
        sets.append("title = ?")
        params.append(_clean_title(payload.get("title")))
    where, where_params = "id = ?", [note_id]
    if "body" in payload:
        body = payload.get("body")
        if not isinstance(body, str):
            raise ApiError("The note must be text.", 400)
        check_text("body", body, NOTE_MAX)
        sets.append("body = ?")
        params.append(body)
        sets.append("version = version + 1")
        if payload.get("base_version") is not None:
            try:
                base = int(payload.get("base_version"))
            except (TypeError, ValueError):
                raise ApiError("Invalid version.", 400)
            where += " AND version = ?"
            where_params.append(base)
    if not sets:
        return {"note": _note_row(_get_note(conn, note_id))}
    sets.append("updated_at = ?")
    params.append(time.time())
    cursor = conn.execute(
        f"UPDATE udb.sticky_notes SET {', '.join(sets)} WHERE {where}", (*params, *where_params)
    )
    if cursor.rowcount == 0:
        conn.commit()
        raise ApiError("This sticky note was changed elsewhere since it was opened.", 409)
    note = _note_row(_get_note(conn, note_id))
    conn.commit()
    return {"note": note}


def conversation_extras(conn, conversation_id: str) -> dict:
    """What the conversation view needs, for the whole conversation (the
    header icons count every page, not just the one loaded)."""
    return {
        "message_edits": conversation_edits(conn, conversation_id),
        "hidden_items": conversation_hidden(conn, conversation_id),
        "sticky_notes": conversation_notes(conn, conversation_id),
    }
