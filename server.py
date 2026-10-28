#!/usr/bin/env python3
"""Minimal HTTP server for Claude History Viewer."""
from __future__ import annotations
import datetime, errno, html, json, mimetypes, os, re, socket, sqlite3, sys, threading, time, urllib.parse, uuid
import importlib
import subprocess
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

# Reusable business/data logic for the labeling features. server.py stays the
# HTTP/routing shell; these modules own the SQL, validation, and transactions
# and raise ApiError for error responses.
import labels, bulk_labels, snapshots, gizmos, updater, advanced_search, related, scratchpads, message_ids
import message_versions
import app_paths, schema_changes, errorlog, backups, request_limits
from request_limits import FIELD_LIMITS, check_text
from api_common import ApiError
import query_rules, conversation_tables, tag_ids, background_jobs
from query_rules import DISPLAY_TITLE, IN_RECYCLE_BIN, META_JOIN, NOT_DELETED

# Set CHV_TIMING=1 to log each request's method, path, and duration to stderr.
# Off by default so normal runs stay quiet; useful for spotting slow endpoints.
_TIMING = os.environ.get("CHV_TIMING", "").strip() not in ("", "0", "false", "no")

try:
    _docx_module = importlib.import_module("docx")
    _DocxDocument = getattr(_docx_module, "Document", None)
    _DOCX_OK = callable(_DocxDocument)
except Exception:
    _DocxDocument = None
    _DOCX_OK = False

def _code_signature() -> str:
    """A cheap fingerprint of the backend code files, read from disk right now.

    Recorded once at import time (the code THIS process is actually running) and
    re-read on demand. If the two differ, server.py (or build_db.py) was
    replaced on disk but the process was never restarted — i.e. the running
    server is older than the files on disk. That is the usual cause of a fix
    that "looks applied but isn't fully": the new front-end is served straight
    from disk, while the old backend process keeps answering the API."""
    import hashlib
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in ("server.py", "build_db.py", "api_common.py",
                 "labels.py", "bulk_labels.py", "snapshots.py", "gizmos.py",
                 "updater.py", "advanced_search.py", "related.py", "scratchpads.py",
                 "message_ids.py", "app_paths.py", "errorlog.py", "backups.py", "request_limits.py",
                 "schema_changes.py", "version.py", "query_rules.py", "conversation_tables.py",
                 "tag_ids.py", "background_jobs.py", "import_lock.py", "message_versions.py"):
        try:
            h.update((here / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def _static_signature() -> str:
    """A fingerprint of the front-end files on disk. The page compares this to
    what it loaded with; a change means the front-end was updated, so the page
    reloads itself to pick it up (no server restart needed for front-end-only
    changes, since static files are served straight from disk)."""
    import hashlib
    static_dir = Path(__file__).resolve().parent / "static"
    h = hashlib.sha256()
    for name in ("index.html", "api.js", "app.js", "accessibility.js", "theme.css", "style.css", "settings.css",
                 "advanced_search.js", "advanced_search.css",
                 "labels_screen.js", "bulk_labels.js", "snapshots.js",
                 "gizmos.js", "bookmarks.js", "bookmarks_hub.js", "notes.js",
                 "summary.js", "summary_editor_core.js", "related.js",
                 "internal_links.js", "scratchpads.js", "scratchpads.css",
                 "message_versions.js", "sticky_notes.js"):
        try:
            h.update((static_dir / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()


_STARTUP_TIME = time.time()
_STARTUP_CODE_SIG = _code_signature()


def _userdata_path(db_path) -> Path:
    """Path to the persistent user-data DB (folders etc.), a sibling of the
    history DB. Kept in its own file so it survives history.db being rebuilt or
    deleted — conversation IDs are stable across rebuilds, so membership holds."""
    return app_paths.userdata_path(db_path)


def open_db(db_path):
    # One short-lived connection per request. With ThreadingHTTPServer several
    # requests can run at once, so give SQLite a busy timeout (waits instead of
    # immediately raising "database is locked") and enable WAL so readers never
    # block the occasional small writer. Each connection is created and closed
    # inside a single request/thread, so check_same_thread stays at its default.
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        # Per-connection, lock-free settings only. journal_mode=WAL is a
        # persistent DB property set once at startup (see _ensure_runtime_schema)
        # — setting it here per request would make many cold connections contend
        # on the WAL switch's exclusive lock.
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error:
        pass  # pragmas are best-effort; never fail a request over them
    try:
        # Attach the persistent user-data DB as schema `udb` so folder tables
        # can be joined against conversations in the same query.
        conn.execute("ATTACH DATABASE ? AS udb", (str(_userdata_path(db_path)),))
    except sqlite3.Error:
        pass
    return conn


def _history_change_1_added_columns(conn) -> None:
    """Columns added to history.db before numbered changes existed. Values
    stay NULL until the next import fills them."""
    for table, col in (
        ("conversations", "import_status TEXT DEFAULT 'normal'"),
        ("conversations", "source_index INTEGER"),
        # Conversation identity is (id, provider). Databases built before this
        # column existed are backfilled at start-up from the dataset format.
        ("conversations", "provider TEXT"),
        # ChatGPT Custom GPT identity.
        ("conversations", "gizmo_id TEXT"),
        ("conversations", "gizmo_type TEXT"),
        # ChatGPT exports identify models per message.
        ("messages", "model_slug TEXT"),
        # The export's own message id (message_ids.py). Older databases get
        # it filled from the backups in source/ on start.
        ("messages", "message_uuid TEXT"),
        # Compare items carry their own provider for tab colouring.
        ("workspace_tabs", "provider TEXT"),
        # 1 only when the user explicitly added the chat to Compare.
        ("workspace_tabs", "compare INTEGER DEFAULT 0"),
    ):
        schema_changes.add_column(conn, table, col)


# The user's own per-conversation state and preferences. history.db kept them
# until change 2 moved them into userdata.db (AUDIT.md #6), so a rebuild of
# history.db no longer wipes them.
MOVED_TO_USERDATA = {
    "conversation_meta": "conversation_id, custom_title, archived, deleted",
    "pinned_conversations": "conversation_id, pinned_at, order_index",
    "ui_preferences": "pref_key, pref_value",
}


def _history_change_2_move_user_state(conn) -> None:
    """Move renames, Archive and Recycle Bin flags, pins and preferences into
    userdata.db (attached as `udb`, its tables already created), then drop
    history.db's copies. A row userdata.db already holds is kept."""
    for table, cols in MOVED_TO_USERDATA.items():
        if not schema_changes.table_exists(conn, table):
            continue
        conn.execute(
            f"INSERT OR IGNORE INTO udb.{table} ({cols}) SELECT {cols} FROM main.{table}"
        )
        conn.execute(f"DROP TABLE main.{table}")


# The Compare bar and the chats held aside for review on the import screen.
# history.db kept them until change 3 moved them into userdata.db (AUDIT.md
# #39), so a rebuild of history.db no longer loses them.
MOVED_TO_USERDATA_3 = {
    "workspace_tabs": "id, tab_type, conversation_id, artifact_id, title, pinned, "
                      "sort_index, last_active_at, closed, provider, compare",
    "pending_reimport": "conversation_id, provider, chat_name, first_message, "
                        "last_message, total_messages, record_json, reason, created_at",
}


def _history_change_3_move_compare_and_review(conn) -> None:
    """Move the Compare bar and the review list into userdata.db, then drop
    history.db's copies. A row userdata.db already holds is kept."""
    for table, cols in MOVED_TO_USERDATA_3.items():
        if not schema_changes.table_exists(conn, table):
            continue
        conn.execute(
            f"INSERT OR IGNORE INTO udb.{table} ({cols}) SELECT {cols} FROM main.{table}"
        )
        conn.execute(f"DROP TABLE main.{table}")


def _history_change_4_artifact_key(conn) -> None:
    """Artifacts are identified by (chat, artifact name), not the name alone
    (AUDIT.md #5). Rebuild the table with that key, keeping every row. The
    artifacts an older import overwrote come back at the next re-import."""
    if not schema_changes.table_exists(conn, "artifacts"):
        return
    keys = [r[1] for r in sorted(conn.execute("PRAGMA table_info(artifacts)"), key=lambda r: r[5]) if r[5]]
    if keys == ["conv_id", "id"]:
        return
    conn.executescript(
        """
        CREATE TABLE artifacts_new (
            id       TEXT NOT NULL,
            conv_id  TEXT NOT NULL,
            msg_seq  INTEGER,
            title    TEXT,
            type     TEXT,
            lang     TEXT,
            content  TEXT,
            PRIMARY KEY (conv_id, id)
        );
        INSERT OR IGNORE INTO artifacts_new (id, conv_id, msg_seq, title, type, lang, content)
            SELECT id, conv_id, msg_seq, title, type, lang, content FROM artifacts;
        DROP TABLE artifacts;
        ALTER TABLE artifacts_new RENAME TO artifacts;
        CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON artifacts (conv_id);
        """
    )


def _history_change_5_memory_ids(conn) -> None:
    """Give each memory an id made from its own text (AUDIT.md #11), so the
    same memory has the same id after a rebuild. The row number still
    changes; never use it to refer to a memory."""
    import build_db
    if not schema_changes.table_exists(conn, "memories"):
        return
    schema_changes.add_column(conn, "memories", "memory_id TEXT")
    for row_id, account, content in conn.execute(
        "SELECT id, account_uuid, content FROM memories WHERE memory_id IS NULL"
    ).fetchall():
        conn.execute("UPDATE memories SET memory_id = ? WHERE id = ?",
                     (build_db.memory_id(account, content), row_id))


def _history_change_6_content_type_counts(conn) -> None:
    """Each imported file records the content it left out and the content
    types it did not know (AUDIT.md #12), shown in the import audit. Files
    imported before this change have no counts until the next re-import."""
    schema_changes.add_column(conn, "imported_backups", "dropped_types TEXT")
    schema_changes.add_column(conn, "imported_backups", "unknown_types TEXT")


# ui_preferences keys the app writes for itself. PATCH /api/preferences never
# changes them, and GET /api/preferences leaves them out.
INTERNAL_PREF_KEYS = frozenset({"dataset_format"})


# history.db change steps, in order. Append new steps; never edit shipped ones.
HISTORY_SCHEMA_STEPS = [
    _history_change_1_added_columns,
    _history_change_2_move_user_state,
    _history_change_3_move_compare_and_review,
    _history_change_4_artifact_key,
    _history_change_5_memory_ids,
    _history_change_6_content_type_counts,
]


# The app page may only run its own scripts and styles, from its own address
# (AUDIT.md #1). KaTeX is kept in static/vendor/ (#15), so maths still renders.
# Images may still come from https addresses, because chat text can link to
# them (markdown images); everything else stays on the app's own address.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data: blob: https:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))

# What the user sees when a request fails for a reason they can't fix. The
# technical detail goes only to logs/errors.json.
_SERVER_ERROR_MESSAGE = ("Something went wrong on the server, so this didn't "
                         "work. The details are in the error log.")


def _busy_or_failed(exc, failed: str) -> str:
    """A plain message for a database error: a locked database means another
    change (usually an import) is running; anything else uses `failed`. The
    technical detail goes to the error log, not the screen (AUDIT.md #23)."""
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return ("This couldn't finish because another change, such as an "
                "import, is running. Try again when it's done.")
    return failed + " The details are in the error log."


def _log_errors(method):
    """Wrap a Handler do_* method: run the front-door checks (Host, and for
    changes the request checks), then the method. Every failure gets a JSON
    reply instead of a dropped connection (AUDIT.md #9): an ApiError gets its
    own status and message, anything else is written to the error log and
    gets a plain-language 500."""
    def wrapper(self):
        self._sent_status = None
        self._logged_body = None
        self._body_read = False
        try:
            problem = self._front_door_problem()
            if problem:
                status, message = problem
                raise ApiError(message, status)
            return method(self)
        except (BrokenPipeError, ConnectionResetError):
            return  # the browser went away; nobody to reply to
        except ApiError as exc:
            if exc.status >= 500:
                self._log_request_error(exc, exc.status)
            self._reply_error(exc.message, exc.status)
        except Exception as exc:
            self._log_request_error(exc, 500)
            self._reply_error(_SERVER_ERROR_MESSAGE, 500)
        finally:
            self._discard_unread_body()
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


def _base_version(payload) -> int | None:
    """The version a Notes or Summary save was based on, or None when the
    caller sent none (then the save is not checked)."""
    if payload.get("base_version") is None:
        return None
    try:
        return int(payload.get("base_version"))
    except (TypeError, ValueError):
        raise ApiError("Invalid version.", 400)


def _check_version(conn, table: str, conv_id: str, base: int | None, message: str) -> None:
    """Refuse a save made from an out-of-date copy, as Scratch Pads do
    (AUDIT.md #34). A chat with no saved row is at version 0."""
    if base is None:
        return
    row = conn.execute(
        f"SELECT COALESCE(version, 0) FROM {table} WHERE conversation_id = ?", (conv_id,)
    ).fetchone()
    if (row[0] if row else 0) != base:
        conn.rollback()
        raise ApiError(message + " Your text is still here; copy it, then reopen.", 409)


def _ensure_runtime_schema(db_path: Path) -> None:
    # userdata.db first: history.db's change 2 moves rows into its tables.
    _ensure_userdata_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Enable WAL once (persistent): readers don't block the occasional small
        # writer, so archive/pin writes can't stall list/detail reads.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        conn.execute("ATTACH DATABASE ? AS udb", (str(_userdata_path(db_path)),))
        # Indexes that speed up the conversation-list sort and detail load.
        # Created at runtime so existing databases benefit without a rebuild.
        # Each is best-effort: a missing base table (partial DB) must not abort
        # the runtime-schema setup above.
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_conv_update ON conversations (update_time DESC, create_time DESC)",
            "CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON artifacts (conv_id)",
            "CREATE INDEX IF NOT EXISTS idx_conv_gizmo ON conversations (gizmo_id)",
            "CREATE INDEX IF NOT EXISTS idx_msg_model ON messages (model_slug, conversation_id)",
        ):
            try:
                conn.execute(idx_sql)
            except sqlite3.Error:
                pass
        # Numbered changes (schema_changes.py): columns added since the first
        # release, applied once each and recorded in schema_version.
        if schema_changes.pending_steps(conn, HISTORY_SCHEMA_STEPS):
            conn.commit()
            # Both files: a history.db change may write to userdata.db too.
            backups.backup_all(db_path, "before-db-change")
        schema_changes.apply_steps(conn, "history.db", HISTORY_SCHEMA_STEPS)
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_msg_model ON messages (model_slug, conversation_id)",
            "CREATE INDEX IF NOT EXISTS idx_msg_uuid ON messages (conversation_id, message_uuid)",
        ):
            try:
                conn.execute(idx_sql)
            except sqlite3.Error:
                pass
        # The top strip is now the Compare bar. Older versions created a tab row
        # every time a chat was opened; none of those were deliberate Compare
        # selections, so drop every non-compare row. Compare additions are
        # written with compare = 1, so this only ever removes the legacy rows
        # and, on a database with nothing selected, leaves the table empty (no
        # reserved space for the strip).
        try:
            conn.execute(
                "DELETE FROM udb.workspace_tabs WHERE COALESCE(compare, 0) = 0"
            )
        except sqlite3.Error:
            pass
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_status ON conversations (import_status)"
            )
        except sqlite3.Error:
            pass
        # Backfill provider on legacy rows from the recorded dataset format.
        try:
            row = conn.execute(
                "SELECT pref_value FROM udb.ui_preferences WHERE pref_key = 'dataset_format'"
            ).fetchone()
            fmt = "claude"
            if row and row[0]:
                try:
                    fmt = json.loads(row[0])
                except Exception:
                    fmt = row[0]
            if fmt not in ("claude", "chatgpt"):
                fmt = "claude"
            conn.execute(
                "UPDATE conversations SET provider = ? WHERE provider IS NULL OR provider = ''",
                (fmt,),
            )
        except sqlite3.Error:
            pass
        # Import-history table: one row per backup brought in via "Import New
        # Chats". Created at runtime so databases built before the feature gain
        # it without a rebuild.
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS imported_backups (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_name    TEXT,
                    provider     TEXT,
                    file_hash    TEXT,
                    first_chat   REAL,
                    last_chat    REAL,
                    total_chats  INTEGER,
                    imported_at  REAL,
                    added        INTEGER DEFAULT 0,
                    updated      INTEGER DEFAULT 0,
                    unchanged    INTEGER DEFAULT 0,
                    skipped      INTEGER DEFAULT 0,
                    errors       INTEGER DEFAULT 0,
                    dropped_types TEXT,
                    unknown_types TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_imported_backups_provider
                    ON imported_backups (provider);
                """
            )
        except sqlite3.Error:
            pass
        conn.execute(
            f"""
            UPDATE udb.workspace_tabs
               SET title = (
                   SELECT {DISPLAY_TITLE}
                     FROM main.conversations c
                     {META_JOIN}
                    WHERE c.id = udb.workspace_tabs.conversation_id
               )
             WHERE tab_type = 'conversation'
               AND conversation_id IS NOT NULL
               AND TRIM(COALESCE(title, '')) = ''
            """
        )
        conn.commit()
    finally:
        conn.close()


def _userdata_change_1_added_columns(conn) -> None:
    """Changes made to userdata.db before numbered changes existed."""
    add = schema_changes.add_column
    # Folders are scoped by provider.
    add(conn, "folders", "provider TEXT")
    # Scratch Pad content version (scratchpads.update_pad).
    add(conn, "scratch_pads", "version INTEGER NOT NULL DEFAULT 0")
    # Bookmarks made before message ids were kept learn theirs on first use.
    add(conn, "conversation_bookmarks", "message_uuid TEXT")
    # Bookmarks used to be unique by position (conversation_id, seq). A
    # bookmark is now identified by its message's id, so a bookmark whose
    # message is gone may share a position with a new one. Rebuild the table
    # without the position rule, carrying every bookmark over.
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'conversation_bookmarks'"
    ).fetchone()
    if table_sql and "UNIQUE(conversation_id, seq)" in (table_sql[0] or ""):
        conn.executescript(
            """
            CREATE TABLE conversation_bookmarks_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                name            TEXT,
                source          TEXT DEFAULT 'user',
                created_at      REAL,
                updated_at      REAL,
                message_uuid    TEXT
            );
            INSERT INTO conversation_bookmarks_new
                SELECT id, conversation_id, seq, name, source, created_at, updated_at, message_uuid
                FROM conversation_bookmarks;
            DROP TABLE conversation_bookmarks;
            ALTER TABLE conversation_bookmarks_new RENAME TO conversation_bookmarks;
            """
        )
    # Fingerprint of the bookmarked message (message_ids.message_fingerprint).
    add(conn, "conversation_bookmarks", "message_hash TEXT")
    # summary column for snapshots created before it existed.
    add(conn, "snapshots", "summary TEXT")
    # Author-tracking columns for conversation summaries.
    add(conn, "conversation_summaries", "condensed_author TEXT")
    add(conn, "conversation_summaries", "summary_author TEXT")
    # Bulk-label rows recorded before "Set label to" became Added/Removed/
    # Cleared carry the action in target_label_id/target_label_name instead;
    # label_action/action_summary just come back NULL for those.
    for tbl in ("label_bulk_history", "label_bulk_runs"):
        add(conn, tbl, "label_action TEXT")
        add(conn, tbl, "action_summary TEXT")
    # Databases built before conversations could carry several labels have
    # conversation_labels keyed on conversation_id alone. Rebuild the table
    # with the (conversation_id, label_id) key, carrying every existing
    # assignment over unchanged.
    cols = conn.execute("PRAGMA table_info(conversation_labels)").fetchall()
    single = any(c[1] == "conversation_id" and c[5] == 1 for c in cols) and len(
        [c for c in cols if c[5] > 0]
    ) == 1
    if single:
        conn.executescript(
            """
            CREATE TABLE conversation_labels_new (
                conversation_id TEXT NOT NULL,
                label_id        TEXT NOT NULL,
                assigned_at     REAL,
                PRIMARY KEY (conversation_id, label_id)
            );
            INSERT OR IGNORE INTO conversation_labels_new
                SELECT conversation_id, label_id, assigned_at
                FROM conversation_labels;
            DROP TABLE conversation_labels;
            ALTER TABLE conversation_labels_new RENAME TO conversation_labels;
            CREATE INDEX IF NOT EXISTS idx_conv_labels_label
                ON conversation_labels (label_id);
            """
        )


def _userdata_change_2_tag_ids(conn) -> None:
    """Give every tag and mood tag an id (AUDIT.md #11)."""
    schema_changes.add_column(conn, "conversation_tags", "tag_id TEXT")
    schema_changes.add_column(conn, "conversation_mood_tags", "tag_id TEXT")
    tag_ids.fill_missing(conn, schema="main")


def _userdata_change_3_note_versions(conn) -> None:
    """Notes and Summaries count their saves, like Scratch Pads, so a save
    from an out-of-date copy is refused (AUDIT.md #34)."""
    schema_changes.add_column(conn, "conversation_notes", "version INTEGER NOT NULL DEFAULT 0")
    schema_changes.add_column(conn, "conversation_summaries", "version INTEGER NOT NULL DEFAULT 0")


def _userdata_change_4_model_ids(conn) -> None:
    """The models from claude_models.json get the file's own ids, so every
    install uses the same id for the same model and "Reload Model Data"
    matches by id, not name (AUDIT.md #11). A model is found by its name
    this one time only."""
    for model_id, name in _seed_model_ids():
        if conn.execute("SELECT 1 FROM claude_models WHERE id = ?", (model_id,)).fetchone():
            continue
        row = conn.execute(
            "SELECT id FROM claude_models WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if not row:
            continue
        old = row[0]
        conn.execute("UPDATE claude_models SET id = ? WHERE id = ?", (model_id, old))
        conn.execute("UPDATE claude_model_periods SET model_id = ? WHERE model_id = ?", (model_id, old))
        conn.execute("UPDATE conversation_models SET model_id = ? WHERE model_id = ?", (model_id, old))


def _userdata_change_5_message_versions(conn) -> None:
    """The user's edits of messages, hidden message versions and sticky
    notes, each keyed by fixed ids (message_versions.py)."""
    message_versions.create_tables(conn)


# userdata.db change steps, in order. Append new steps; never edit shipped ones.
USERDATA_SCHEMA_STEPS = [
    _userdata_change_1_added_columns,
    _userdata_change_2_tag_ids,
    _userdata_change_3_note_versions,
    _userdata_change_4_model_ids,
    _userdata_change_5_message_versions,
]


def _ensure_userdata_schema(db_path: Path) -> None:
    """Create the persistent user-data DB (folders + folder membership)."""
    conn = sqlite3.connect(_userdata_path(db_path))
    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at REAL,
                updated_at REAL,
                -- Which side the folder belongs to: folders are scoped to a
                -- provider so the Claude and ChatGPT sidebars stay separate.
                provider   TEXT
            );
            CREATE TABLE IF NOT EXISTS folder_items (
                conversation_id TEXT PRIMARY KEY,
                folder_id       TEXT NOT NULL,
                added_at        REAL,
                pinned          INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_folder_items_folder ON folder_items (folder_id);

            -- Claude model availability. A model is one row in claude_models;
            -- each window it was offered in claude.ai is one row in
            -- claude_model_periods, so a model that came back after a gap
            -- (Claude Fable 5) is one model with two period rows.
            CREATE TABLE IF NOT EXISTS claude_models (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS claude_model_periods (
                id         TEXT PRIMARY KEY,
                model_id   TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date   TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_model_periods ON claude_model_periods (model_id);

            -- Which models the user says a conversation used. Many-to-many.
            CREATE TABLE IF NOT EXISTS conversation_models (
                conversation_id TEXT NOT NULL,
                model_id        TEXT NOT NULL,
                added_at        REAL,
                PRIMARY KEY (conversation_id, model_id)
            );

            -- Per-conversation "I checked this, stop warning me" state. Cleared
            -- automatically when the warning's condition stops being true, so
            -- the icon comes back if the dates drift out of range again.
            CREATE TABLE IF NOT EXISTS conversation_model_flags (
                conversation_id    TEXT PRIMARY KEY,
                range_dismissed    INTEGER DEFAULT 0,
                coverage_dismissed INTEGER DEFAULT 0
            );

            -- Bookkeeping for the one-time seed import (see _seed_claude_models).
            CREATE TABLE IF NOT EXISTS udb_meta (
                meta_key   TEXT PRIMARY KEY,
                meta_value TEXT
            );

            -- Free-form tags a user attaches to a conversation. Lives here (not
            -- in history.db) so tags survive a rebuild —
            -- conversation IDs are stable across rebuilds, so membership holds.
            CREATE TABLE IF NOT EXISTS conversation_tags (
                conversation_id TEXT NOT NULL,
                tag             TEXT NOT NULL,
                added_at        REAL,
                PRIMARY KEY (conversation_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_conv_tags_tag ON conversation_tags (tag);

            -- Mood tags: a second, independent per-conversation tag set. Same
            -- persistence model as ordinary tags, but never mixed with them.
            CREATE TABLE IF NOT EXISTS conversation_mood_tags (
                conversation_id TEXT NOT NULL,
                tag             TEXT NOT NULL,
                added_at        REAL,
                PRIMARY KEY (conversation_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_conv_mood_tags_tag
                ON conversation_mood_tags (tag);

            -- User-assigned names for ChatGPT Custom GPT ids. Kept outside
            -- history.db so naming survives rebuilds and applies to every chat
            -- carrying the same imported gizmo id.
            CREATE TABLE IF NOT EXISTS gizmo_names (
                gizmo_id     TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                updated_at   REAL
            );

            -- A lightweight tombstone left behind when a conversation is
            -- permanently deleted from the Recycle Bin. It holds no message
            -- content — only enough identity/metadata for a later import to
            -- recognise the conversation was deliberately deleted and decide
            -- whether to re-import it. Lives here (not history.db) so it
            -- survives a rebuild; identity is (conversation_id, provider), the
            -- same rule the importer uses everywhere else.
            CREATE TABLE IF NOT EXISTS deleted_records (
                conversation_id TEXT NOT NULL,
                provider        TEXT NOT NULL,
                chat_name       TEXT,
                first_message   REAL,
                last_message    REAL,
                total_messages  INTEGER,
                deleted_at      REAL,
                PRIMARY KEY (conversation_id, provider)
            );

            -- User-configurable conversation labels. A label is a definition
            -- (name/colour/order) here; a conversation's assignment lives in
            -- conversation_labels below. Names carry no hard-coded meaning —
            -- the user chooses them — and the stable id means renaming or
            -- recolouring a label never disturbs which conversations use it.
            CREATE TABLE IF NOT EXISTS labels (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                color      TEXT,
                -- Position in the click-cycle / display order (0-based).
                sort_index INTEGER NOT NULL DEFAULT 0,
                created_at REAL,
                updated_at REAL
            );

            -- Which labels a conversation carries. A conversation can carry
            -- several; the blank state is simply the absence of any row (no
            -- fake "Blank" label). Keyed on (conversation_id, label_id) so one
            -- conversation can hold many labels and no label twice.
            -- Conversation ids are stable across rebuilds, so assignments
            -- survive a history.db rebuild.
            CREATE TABLE IF NOT EXISTS conversation_labels (
                conversation_id TEXT NOT NULL,
                label_id        TEXT NOT NULL,
                assigned_at     REAL,
                PRIMARY KEY (conversation_id, label_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conv_labels_label
                ON conversation_labels (label_id);

            -- Related Conversations: one row per reciprocal link. The pair is
            -- stored in sorted order (conversation_a < conversation_b), so
            -- linking A to B and B to A is the same row. See related.py.
            CREATE TABLE IF NOT EXISTS conversation_links (
                conversation_a TEXT NOT NULL,
                conversation_b TEXT NOT NULL,
                linked_at      REAL,
                PRIMARY KEY (conversation_a, conversation_b)
            );
            CREATE INDEX IF NOT EXISTS idx_conv_links_b
                ON conversation_links (conversation_b);

            -- A short, durable log of each successful conditional bulk-label
            -- operation. Not an undo journal — it holds no conversation bodies
            -- and no per-conversation before/after state, only enough to remind
            -- the user what they did and to let them reuse a prior selection.
            -- Recovery from a bad change is the job of snapshots, not this.
            CREATE TABLE IF NOT EXISTS label_bulk_history (
                id                TEXT PRIMARY KEY,
                created_at        REAL,
                -- The selection criteria, serialized, so it can be reloaded.
                criteria_json     TEXT,
                -- A human-readable summary captured at execution time.
                description       TEXT,
                -- The label applied; NULL means "cleared to blank".
                target_label_id   TEXT,
                -- The label's name at the time, kept even if it is later renamed
                -- or deleted so old history stays readable.
                target_label_name TEXT,
                limit_used        INTEGER,
                -- Sorting/order settings used to pick which matches the limit hit.
                order_json        TEXT,
                eligible_count    INTEGER,
                changed_count     INTEGER
            );

            -- A saved, partially-processed conditional bulk-label operation.
            -- Created when an Apply's "First N" limit leaves some of the
            -- qualifying conversations unprocessed. The qualifying set and its
            -- order are frozen at creation (ids_json) so later Continues never
            -- re-run the criteria against current data — they only slice
            -- further into this same frozen list and advance completed_count.
            -- Deleted once the whole list has been processed (a Recent Bulk
            -- Changes row is written at that point) or if the user deletes it.
            CREATE TABLE IF NOT EXISTS label_bulk_runs (
                id                  TEXT PRIMARY KEY,
                created_at          REAL,
                criteria_json       TEXT,
                -- Read-only "Criteria:" lines shown in Unfinished Label Runs,
                -- frozen at creation like label_bulk_history.description.
                criteria_lines_json TEXT,
                order_desc          TEXT,
                target_label_id     TEXT,
                target_label_name   TEXT,
                total_qualifying    INTEGER,
                completed_count     INTEGER,
                batch_size          INTEGER,
                -- The full ordered set of qualifying conversation ids, frozen
                -- at creation time.
                ids_json            TEXT
            );

            -- Manual safety snapshots of user-owned metadata (never message
            -- content or imported source text). Capped at 10 rows; creation is
            -- blocked at the cap until the user deletes one (nothing is ever
            -- deleted silently). The payload is versioned JSON so new categories
            -- can be added later without invalidating old snapshots.
            CREATE TABLE IF NOT EXISTS snapshots (
                id             TEXT PRIMARY KEY,
                name           TEXT,
                created_at     REAL,
                schema_version INTEGER,
                payload        TEXT,
                -- Optional display metadata computed at capture time.
                item_count     INTEGER,
                payload_size   INTEGER,
                -- Per-category counts (JSON) for the list's Contents column, so
                -- listing never has to parse the full payload.
                summary        TEXT
            );

            -- Per-conversation user-authored summaries. Both fields are
            -- optional; hiding the condensed-summary setting does not delete
            -- data — it only controls visibility.
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id    TEXT PRIMARY KEY,
                condensed_summary  TEXT,
                summary            TEXT,
                condensed_author   TEXT,
                summary_author     TEXT,
                updated_at         REAL
            );
            -- Three distinct, user-editable note fields for each conversation.
            -- They deliberately remain separate so future API operations can
            -- read Notes to the AI and write Notes from the AI independently.
            CREATE TABLE IF NOT EXISTS conversation_notes (
                conversation_id TEXT PRIMARY KEY,
                user_notes      TEXT,
                notes_to_ai     TEXT,
                notes_from_ai   TEXT,
                updated_at      REAL
            );
            -- Per-message bookmarks. Each bookmark carries its own stable id
            -- (a UUID) that never changes when it is renamed, so later features
            -- can reference a bookmark steadily regardless of its display name.
            -- The conversation's own id is untouched by any of this. `source`
            -- records who created the bookmark ("user" today; leaves room for an
            -- "ai" origin later, which may show a different icon).
            CREATE TABLE IF NOT EXISTS conversation_bookmarks (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                name            TEXT,
                source          TEXT DEFAULT 'user',
                created_at      REAL,
                updated_at      REAL,
                -- The bookmarked message's export id (message_ids.py). It is
                -- the bookmark's identity; seq follows it and is unique per
                -- message only through this id (see the indexes below).
                message_uuid    TEXT,
                -- Fingerprint of the message's role and text, so a bookmark
                -- recognises its message after a rebuild even without an id.
                message_hash    TEXT
            );

            -- Advanced searches intentionally live in the portable user-data
            -- database. Recent searches survive restarts; saved searches keep
            -- their stable id and user-supplied name across copied installs.
            CREATE TABLE IF NOT EXISTS advanced_search_recent (
                id            TEXT PRIMARY KEY,
                criteria_json TEXT NOT NULL,
                created_at    REAL NOT NULL,
                last_used_at  REAL NOT NULL,
                use_count     INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_advanced_recent_used
                ON advanced_search_recent (last_used_at DESC);
            -- Scratch Pads: free-standing Markdown documents that belong to no
            -- conversation and to neither provider. The id is generated once
            -- and never changes on rename. See scratchpads.py.
            CREATE TABLE IF NOT EXISTS scratch_pads (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                content    TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                version    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_scratch_pads_updated
                ON scratch_pads (updated_at DESC);
            CREATE TABLE IF NOT EXISTS advanced_search_saved (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                criteria_json TEXT NOT NULL,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
            -- The user's rename, Archive and Recycle Bin state for each
            -- conversation. A conversation with no row is active and keeps its
            -- imported title. Moved here from history.db (history change 2).
            CREATE TABLE IF NOT EXISTS conversation_meta (
                conversation_id TEXT PRIMARY KEY,
                custom_title    TEXT,
                archived        INTEGER DEFAULT 0,
                deleted         INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_meta_flags ON conversation_meta (deleted, archived);
            -- Loose pins at the top of the sidebar.
            CREATE TABLE IF NOT EXISTS pinned_conversations (
                conversation_id TEXT PRIMARY KEY,
                pinned_at       REAL,
                order_index     INTEGER
            );
            -- Screen settings, plus `dataset_format` (which side the first
            -- export came from), written by build_db.
            CREATE TABLE IF NOT EXISTS ui_preferences (
                pref_key   TEXT PRIMARY KEY,
                pref_value TEXT
            );
            -- The chats on the Compare bar. Moved here from history.db
            -- (history change 3) so a rebuild keeps them.
            CREATE TABLE IF NOT EXISTS workspace_tabs (
                id              TEXT PRIMARY KEY,
                tab_type        TEXT NOT NULL,
                conversation_id TEXT,
                artifact_id     TEXT,
                title           TEXT,
                pinned          INTEGER DEFAULT 0,
                sort_index      INTEGER DEFAULT 0,
                last_active_at  REAL,
                closed          INTEGER DEFAULT 0,
                -- Compare items cross providers, so each keeps its own side.
                provider        TEXT,
                -- 1 only when the user explicitly added the chat to Compare.
                compare         INTEGER DEFAULT 0
            );
            -- Previously-deleted conversations held aside from an import for the
            -- user to review before re-importing (the "Review" import choice,
            -- and the "has new messages" case of the skip choice). Holds the
            -- full parsed record as JSON so it can be imported later without
            -- re-reading the (now renamed) backup file. Moved here from
            -- history.db (history change 3): the backup is already marked as
            -- imported, so a rebuild could never offer these chats again.
            CREATE TABLE IF NOT EXISTS pending_reimport (
                conversation_id TEXT NOT NULL,
                provider        TEXT NOT NULL,
                chat_name       TEXT,
                first_message   REAL,
                last_message    REAL,
                total_messages  INTEGER,
                record_json     TEXT,
                reason          TEXT,
                created_at      REAL,
                PRIMARY KEY (conversation_id, provider)
            );
            -- Each tag and mood tag has an id that never changes (AUDIT.md
            -- #11). conversation_tags / conversation_mood_tags keep the text
            -- too, and point at the id in tag_id. See tag_ids.py.
            CREATE TABLE IF NOT EXISTS tags (
                id         TEXT PRIMARY KEY,
                kind       TEXT NOT NULL,   -- 'tag' or 'mood'
                name       TEXT NOT NULL,
                created_at REAL,
                UNIQUE (kind, name)
            );
            """
        )
        # Numbered changes (schema_changes.py), applied once each and recorded
        # in schema_version.
        if schema_changes.pending_steps(conn, USERDATA_SCHEMA_STEPS):
            conn.commit()
            backups.backup_file(_userdata_path(db_path), "before-db-change")
        schema_changes.apply_steps(conn, "userdata.db", USERDATA_SCHEMA_STEPS)
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_bookmarks_conv_seq ON conversation_bookmarks (conversation_id, seq)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmarks_message "
            "ON conversation_bookmarks (conversation_id, message_uuid) WHERE message_uuid IS NOT NULL",
            # A message without an id is identified by its position, so only
            # one bookmark may sit there (gone bookmarks have a non-null id).
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmarks_position "
            "ON conversation_bookmarks (conversation_id, seq) WHERE message_uuid IS NULL",
        ):
            try:
                conn.execute(idx_sql)
            except sqlite3.Error:
                pass
        conn.execute(
            "UPDATE conversation_meta SET custom_title = NULL WHERE TRIM(COALESCE(custom_title, '')) = ''"
        )
        conn.commit()
        _seed_claude_models(conn)
    finally:
        conn.close()


def _backfill_folder_providers(db_path: Path) -> None:
    """Give every pre-existing folder a provider. A folder that already holds a
    conversation inherits that conversation's provider; an empty one falls back
    to the dataset's own format. Runs once at startup, then no-ops."""
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT pref_value FROM udb.ui_preferences WHERE pref_key = 'dataset_format'"
        ).fetchone()
        fmt = "claude"
        if row and row[0]:
            try:
                fmt = json.loads(row[0])
            except Exception:
                fmt = row[0]
        if fmt not in ("claude", "chatgpt"):
            fmt = "claude"
        conn.execute(
            "UPDATE udb.folders SET provider = ("
            "  SELECT c.provider FROM udb.folder_items fi "
            "  JOIN conversations c ON c.id = fi.conversation_id "
            "  WHERE fi.folder_id = udb.folders.id AND c.provider IS NOT NULL "
            "  LIMIT 1"
            ") WHERE provider IS NULL OR provider = ''"
        )
        conn.execute(
            "UPDATE udb.folders SET provider = ? WHERE provider IS NULL OR provider = ''",
            (fmt,),
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


# ── Claude model availability ────────────────────────────────────────────────

MODEL_SEED_FILE = Path(__file__).parent / "claude_models.json"


def _seed_model_ids() -> list[tuple[str, str]]:
    """(id, name) for each model in claude_models.json, once each."""
    try:
        with open(MODEL_SEED_FILE, encoding="utf-8") as f:
            rows = json.load(f).get("models") or []
    except Exception:
        return []
    out: dict[str, str] = {}
    for entry in rows:
        model_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if model_id and name:
            out.setdefault(model_id, name)
    return list(out.items())


def _seed_claude_models(conn) -> None:
    """Import claude_models.json once, then never again.

    The JSON ships with the app only as a starting list. Once imported, the
    user-data DB is the single source of truth: it travels with the user's
    other data, and their edits are never overwritten by the shipped file.
    The seed is also skipped if the user has emptied the table on purpose.
    """
    try:
        done = conn.execute(
            "SELECT meta_value FROM udb_meta WHERE meta_key = 'claude_models_seeded'"
        ).fetchone()
    except sqlite3.Error:
        return
    if done:
        return
    rows = []
    try:
        with open(MODEL_SEED_FILE, encoding="utf-8") as f:
            rows = json.load(f).get("models") or []
    except Exception:
        rows = []  # a missing or unreadable seed just means an empty list
    now = time.time()
    by_name: dict[str, str] = {}
    for entry in rows:
        name = str(entry.get("name") or "").strip()
        start = str(entry.get("start_date") or "").strip()
        if not name or not start:
            continue
        end = entry.get("end_date")
        end = str(end).strip() if end else None
        key = str(entry.get("id") or "").strip() or name.casefold()
        model_id = by_name.get(key)
        if model_id is None:
            model_id = str(entry.get("id") or "").strip() or uuid.uuid4().hex
            by_name[key] = model_id
            conn.execute(
                "INSERT INTO claude_models(id, name, created_at) VALUES (?, ?, ?)",
                (model_id, name, now),
            )
        conn.execute(
            "INSERT INTO claude_model_periods(id, model_id, start_date, end_date, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, model_id, start, end, now),
        )
    conn.execute(
        "INSERT OR REPLACE INTO udb_meta(meta_key, meta_value) VALUES ('claude_models_seeded', ?)",
        (str(now),),
    )
    conn.commit()


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm_date(value) -> str | None:
    """Accept a YYYY-MM-DD string, reject anything else. Dates are compared as
    strings throughout, which is exactly right for zero-padded ISO dates."""
    s = str(value or "").strip()
    return s if _DATE_RE.match(s) else None


def _ts_to_date(ts) -> str | None:
    """Epoch seconds -> local YYYY-MM-DD, matching the date the UI displays."""
    try:
        return datetime.date.fromtimestamp(float(ts)).isoformat()
    except Exception:
        return None


def _conv_date_range(create_time, update_time):
    """The span a conversation covers, as inclusive (first_day, last_day).

    Returns (None, None) when the export carries no usable timestamps; callers
    treat that as "cannot judge availability" and raise no warnings.
    """
    a = _ts_to_date(create_time)
    b = _ts_to_date(update_time)
    days = [d for d in (a, b) if d]
    if not days:
        return None, None
    return min(days), max(days)


def _period_overlaps(start, end, first_day, last_day) -> bool:
    """Does [start, end) overlap the inclusive day span [first_day, last_day]?

    end is exclusive (the model list's stated interval semantics) and None
    means "still available".
    """
    if start > last_day:
        return False
    return end is None or end > first_day


def _periods_cover(periods, first_day, last_day) -> bool:
    """Do these [start, end) windows together cover every day of the span?"""
    OPEN = "9999-12-31"
    # The span is inclusive of last_day, so coverage must reach past it.
    target = _day_after(last_day)
    reached = first_day
    for start, end in sorted(periods, key=lambda p: p[0]):
        if start > reached:
            break  # gap before this window: the span is not fully covered
        stop = end or OPEN
        if stop > reached:
            reached = stop
        if reached >= target:
            return True
    return reached >= target


def _day_after(day: str) -> str:
    try:
        d = datetime.date.fromisoformat(day) + datetime.timedelta(days=1)
        return d.isoformat()
    except Exception:
        return day


def _load_model_periods(conn) -> dict[str, list[tuple[str, str | None]]]:
    """model_id -> its [start, end) windows."""
    out: dict[str, list[tuple[str, str | None]]] = {}
    try:
        rows = conn.execute(
            "SELECT model_id, start_date, end_date FROM udb.claude_model_periods"
        ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        out.setdefault(r["model_id"], []).append((r["start_date"], r["end_date"]))
    return out


def _conversation_warnings(selected_periods, first_day, last_day):
    """The two warning conditions for one conversation.

    out_of_range: at least one selected model has no window overlapping the
                  conversation at all.
    incomplete:   the selected models together do not cover the conversation
                  from its first day to its last.

    Both are False when nothing is selected — an unset conversation is simply
    unknown, not wrong.
    """
    if not selected_periods or not first_day:
        return False, False
    out_of_range = any(
        not any(_period_overlaps(s, e, first_day, last_day) for s, e in periods)
        for periods in selected_periods
    )
    flat = [p for periods in selected_periods for p in periods]
    incomplete = not _periods_cover(flat, first_day, last_day)
    return out_of_range, incomplete


def _compute_conv_flags(conn, conv_rows):
    """Effective warning icons for a batch of conversations.

    conv_rows: iterable of (conversation_id, create_time, update_time).
    Returns {conversation_id: {"warn_range": bool, "warn_coverage": bool}}.

    Dismissals are re-armed here: a dismissal only suppresses a warning whose
    condition is currently true, so a dismissed warning that later becomes
    accurate again is forgotten, and the icon returns if it goes wrong again.
    """
    rows = [r for r in conv_rows if r[0]]
    if not rows:
        return {}
    try:
        sel_rows = conn.execute(
            "SELECT conversation_id, model_id FROM udb.conversation_models"
        ).fetchall()
    except sqlite3.Error:
        return {}
    selections: dict[str, list[str]] = {}
    for r in sel_rows:
        selections.setdefault(r["conversation_id"], []).append(r["model_id"])
    if not selections:
        return {}
    periods_by_model = _load_model_periods(conn)
    dismissed = {}
    try:
        for r in conn.execute(
            "SELECT conversation_id, range_dismissed, coverage_dismissed "
            "FROM udb.conversation_model_flags"
        ).fetchall():
            dismissed[r["conversation_id"]] = (
                bool(r["range_dismissed"]), bool(r["coverage_dismissed"])
            )
    except sqlite3.Error:
        pass

    out, stale = {}, []
    for conv_id, create_time, update_time in rows:
        model_ids = selections.get(conv_id)
        if not model_ids:
            continue
        first_day, last_day = _conv_date_range(create_time, update_time)
        oor, inc = _conversation_warnings(
            [periods_by_model.get(mid, []) for mid in model_ids], first_day, last_day
        )
        d_range, d_cov = dismissed.get(conv_id, (False, False))
        # Re-arm: a dismissal for a condition that no longer holds is dropped.
        if (d_range and not oor) or (d_cov and not inc):
            stale.append((conv_id, 1 if (d_range and oor) else 0,
                          1 if (d_cov and inc) else 0))
            d_range, d_cov = d_range and oor, d_cov and inc
        out[conv_id] = {
            "warn_range": bool(oor and not d_range),
            "warn_coverage": bool(inc and not d_cov),
        }
    for conv_id, keep_range, keep_cov in stale:
        try:
            if keep_range or keep_cov:
                conn.execute(
                    "UPDATE udb.conversation_model_flags "
                    "SET range_dismissed = ?, coverage_dismissed = ? WHERE conversation_id = ?",
                    (keep_range, keep_cov, conv_id),
                )
            else:
                conn.execute(
                    "DELETE FROM udb.conversation_model_flags WHERE conversation_id = ?",
                    (conv_id,),
                )
        except sqlite3.Error:
            pass
    if stale:
        try:
            conn.commit()
        except sqlite3.Error:
            pass
    return out


def _flagged_conv_ids(conn) -> list[str]:
    """Every conversation currently showing at least one warning icon."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT c.id, c.create_time, c.update_time "
            "FROM conversations c JOIN udb.conversation_models m ON m.conversation_id = c.id"
        ).fetchall()
    except sqlite3.Error:
        return []
    flags = _compute_conv_flags(conn, [(r["id"], r["create_time"], r["update_time"]) for r in rows])
    return [cid for cid, f in flags.items() if f["warn_range"] or f["warn_coverage"]]

def _norm_filename(s: str) -> str:
    """Normalize for matching: lowercase + spaces→underscores."""
    return unicodedata.normalize("NFC", s).lower().replace(" ", "_")

def _extract_snippet(text: str, q: str, radius: int = 100) -> str:
    """Return a plain-text snippet of `text` centered around query `q`."""
    if not text:
        return ""
    idx = text.lower().find(q.lower())
    if idx == -1:
        return text[:radius * 2] + ("…" if len(text) > radius * 2 else "")
    start = max(0, idx - radius // 2)
    end   = min(len(text), idx + len(q) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix

_SOURCE_INDEX: dict[str, list[Path]] = {}


def _source_index_add(key: str, path: Path) -> None:
    if not key:
        return
    bucket = _SOURCE_INDEX.setdefault(unicodedata.normalize("NFC", key), [])
    if path not in bucket:
        bucket.append(path)

def _build_source_index(source_dir):
    _SOURCE_INDEX.clear()
    if not source_dir.exists():
        return
    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            p = Path(root) / fname
            rel = str(p.relative_to(source_dir)).replace("\\", "/")
            rel_stem = str(Path(rel).with_suffix(""))
            _source_index_add(fname, p)
            _source_index_add(p.stem, p)
            _source_index_add(rel, p)
            _source_index_add(rel_stem, p)

# ── Import New Chats (scan / detect / merge / rename) ─────────────────────────

def _resolve_backup_name(source_dir: Path, this_path: Path, base: str, file_hash: str):
    """
    Pick the final filename for a backup and detect an exact duplicate.

    Returns (final_name, is_duplicate). Starts from `base.json`; if that name is
    taken by a *different* file it compares hashes: a match means the identical
    backup is already present (skip the import), a mismatch is a date collision
    so it tries -1, -2, … until a free name is found.
    """
    import hashlib
    n = 0
    while True:
        name = f"{base}.json" if n == 0 else f"{base}-{n}.json"
        p = source_dir / name
        if not p.exists():
            return name, False
        try:
            if p.samefile(this_path):
                return name, False  # this very file is already correctly named
        except OSError:
            pass
        try:
            other_hash = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            other_hash = None
        if other_hash == file_hash:
            return name, True
        n += 1


def _record_dates(record: dict) -> tuple:
    """First-message date, last-message date and total-message count for one
    importer record — derived from its messages, falling back to the
    conversation's own create/update times when the messages carry none. Used
    to fill a review row and to compare an incoming backup against a stored
    permanent-deletion record."""
    meta = record.get("meta") or {}
    msgs = record.get("msgs") or []
    times = [m.get("create_time") for m in msgs if m.get("create_time")]
    first = min(times) if times else meta.get("create_time")
    last = max(times) if times else meta.get("update_time")
    total = len(msgs) if msgs else (meta.get("message_count") or 0)
    return first, last, total


def _main_db_path(conn) -> Path:
    """The file behind a connection's main database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return app_paths.DB_PATH


def import_new_backups(conn, source_dir: Path,
                       deleted_mode: str = "skip",
                       review_new_messages: bool = False,
                       skip_binned: bool = True, job=None) -> dict:
    """
    Scan `source_dir` for conversation backups not yet imported, identify each
    by its contents, merge it into the database, and rename it to
    Provider-conversations-YYYY-MM-DD.json. Returns a summary the UI shows.

    `deleted_mode` controls what happens to conversations that were previously
    permanently deleted (they carry a record in udb.deleted_records):
      • "skip"   — don't re-import them. With `review_new_messages`, a backup
                   whose last message is newer than the deleted version is held
                   aside for review instead of being skipped silently.
      • "all"    — re-import them normally with the rest of the backup and clear
                   their deletion record.
      • "review" — import the rest of the backup now, but hold every previously
                   deleted conversation aside for the user to review.
    Held-aside conversations are stored in pending_reimport (with their full
    record) and surfaced in the import screen's review area.

    `skip_binned=False` (used by rebuild_history) also updates chats in the
    Recycle Bin; they stay in the Recycle Bin.
    """
    if deleted_mode not in ("skip", "all", "review"):
        deleted_mode = "skip"
    import build_db

    summary = {
        "added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0,
        # Conversations held aside for review this run (Review choice, or a
        # previously deleted chat with new messages under the skip choice).
        "review": 0,
        "files": [], "notes": [],
    }
    if job:
        job.result = summary
    processed = 0
    known_hashes = set()
    for row in conn.execute("SELECT file_hash FROM imported_backups"):
        if row[0]:
            known_hashes.add(row[0])

    # Permanent-deletion records, keyed by identity (id, provider) → last-message
    # date, so we can recognise a previously deleted conversation in a backup.
    deleted_map: dict = {}
    for row in conn.execute(
        "SELECT conversation_id, provider, last_message FROM udb.deleted_records"
    ):
        deleted_map[(row[0], row[1])] = row[2]

    # Only top-level *.json files are candidates. memories/users are export
    # siblings, never conversation backups.
    candidates = sorted(p for p in source_dir.glob("*.json") if p.is_file())
    for path in candidates:
        if job:
            job.progress("Checking export files…", done=0, force=True)
        name = path.name
        if name in ("memories.json", "users.json"):
            continue
        try:
            file_hash = build_db.file_sha256(path, job=job)
        except OSError as e:
            errorlog.record_exception(e, action="Read conversation export")
            summary["notes"].append(f"{name}: could not read this file — skipped. See the Error Log.")
            continue
        if file_hash in known_hashes:
            continue  # exact same file already imported

        # The file is read one chat at a time, twice: first to check it is
        # valid and pick the copy kept for each id, then to write those
        # copies. The whole export is never held in memory (AUDIT.md #31).
        try:
            provider = build_db.export_provider(path)
        except ValueError:
            summary["notes"].append(f"{name}: not valid JSON — skipped")
            continue
        except OSError as e:
            errorlog.record_exception(e, action="Read conversation export")
            summary["notes"].append(f"{name}: could not read this file — skipped. See the Error Log.")
            continue
        if provider is None:
            summary["notes"].append(
                f"{name}: not a recognized Claude or ChatGPT export — skipped"
            )
            continue

        try:
            scan = build_db.scan_export(path, provider, job=job)
        except ValueError:
            summary["notes"].append(f"{name}: not valid JSON — skipped")
            continue
        except OSError as e:
            errorlog.record_exception(e, action="Read conversation export")
            summary["notes"].append(f"{name}: could not read this file — skipped. See the Error Log.")
            continue
        winners = scan["winners"]
        if not winners:
            summary["notes"].append(f"{name}: no conversations found — skipped")
            continue
        first_ts, last_ts = scan["first"], scan["last"]

        if name == "conversations.json":
            # The seed export keeps its canonical name: the documented
            # `rm -f history.db && python3 app.py` rebuild needs that exact path.
            # It is still hash-recorded below, so it imports at most once and is
            # skipped on every later scan. (Fresh builds record it up front and
            # never reach this loop; this covers databases built beforehand.)
            final_name, is_dup = name, False
        else:
            date_str = _ts_to_date(last_ts) or datetime.date.today().isoformat()
            label = "Claude" if provider == "claude" else "ChatGPT"
            base = f"{label}-conversations-{date_str}"
            final_name, is_dup = _resolve_backup_name(source_dir, path, base, file_hash)
        if is_dup:
            summary["notes"].append(
                f"{name}: identical to already-imported {final_name} — skipped"
            )
            known_hashes.add(file_hash)
            continue

        # Refuse before writing anything when the disk is too full.
        build_db.check_free_space(_main_db_path(conn), path.stat().st_size)

        # Each kept record is split against the permanent-deletion records,
        # so previously deleted conversations are handled per the chosen mode
        # and never silently resurrected, then written on its own.
        counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        held = 0
        skipped_deleted = 0     # previously deleted, skipped silently
        if job:
            job.progress("Saving chats from " + name + "…", done=0, total=len(winners), force=True)
        file_done = 0
        for rec in build_db.iter_records(path, provider):
            if job:
                job.check()
            if winners.get(rec["meta"]["id"]) != rec["index"]:
                continue  # a shorter copy of a chat this file holds twice
            try:
                key = (rec["meta"]["id"], provider)
                # Other user actions can run between batches. Read deletion
                # state again under this batch's write lock, so a newly purged
                # chat cannot be brought back by a stale initial snapshot.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                deleted_now = conn.execute(
                    "SELECT last_message FROM udb.deleted_records WHERE conversation_id = ? AND provider = ?",
                    key).fetchone()
                if deleted_now is None:
                    deleted_map.pop(key, None)
                else:
                    deleted_map[key] = deleted_now[0]
                reason = None
                if key in deleted_map:
                    if deleted_mode == "review":
                        reason = "review"
                    elif deleted_mode == "skip":
                        _f, inc_last, _t = _record_dates(rec)
                        stored_last = deleted_map[key]
                        if (review_new_messages and inc_last is not None
                                and stored_last is not None
                                and float(inc_last) > float(stored_last) + 1.0):
                            reason = "new_messages"
                        else:
                            skipped_deleted += 1
                            summary["skipped"] += 1
                            continue
                if reason:
                    # Hold it aside with its full record for later.
                    first, last, total = _record_dates(rec)
                    conn.execute(
                        "INSERT OR REPLACE INTO udb.pending_reimport "
                        "(conversation_id, provider, chat_name, first_message, "
                        " last_message, total_messages, record_json, reason, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (rec["meta"]["id"], provider, rec["meta"].get("title") or "Untitled",
                         first, last, total, json.dumps(rec, ensure_ascii=False),
                         reason, time.time()),
                    )
                    held += 1
                    summary["review"] += 1
                    continue
                one = build_db.reconcile_backup(conn, [rec], provider, skip_binned)
                for k in counts:
                    counts[k] += one[k]
                    summary[k] += one[k]
                if key in deleted_map and not one["errors"] and not one["skipped"]:
                    # Mode "all": a re-imported conversation is no longer deleted.
                    conn.execute(
                        "DELETE FROM udb.deleted_records "
                        "WHERE conversation_id = ? AND provider = ?",
                        key,
                    )
                    deleted_map.pop(key, None)
            finally:
                file_done += 1
                processed += 1
                if job:
                    job.state.update(progress=file_done, total=len(winners))
                    job.batch(conn, processed)

        counts["skipped"] += skipped_deleted
        counts["review"] = held
        if job:
            job.check()
        final_path = source_dir / final_name
        try:
            if final_path != path:
                path.rename(final_path)
        except OSError as exc:
            errorlog.record_exception(exc, action="Rename imported export")
            final_name = name  # keep the original name if the rename fails

        conn.execute(
            "INSERT INTO imported_backups "
            "(file_name, provider, file_hash, first_chat, last_chat, "
            " total_chats, imported_at, added, updated, unchanged, skipped, errors, "
            " dropped_types, unknown_types) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (final_name, provider, file_hash, first_ts, last_ts,
             len(winners), time.time(),
             counts["added"], counts["updated"], counts["unchanged"],
             counts["skipped"], counts["errors"],
             json.dumps(scan["content_types"]["dropped"]),
             json.dumps(scan["content_types"]["unknown"])),
        )
        conn.commit()
        known_hashes.add(file_hash)
        summary["files"].append({
            "file_name": final_name, "provider": provider,
            "total_chats": len(winners), **counts,
        })

    return summary


# ── Rebuild (history.db made again from source/) ─────────────────────────────


def _rebuild_seed(source_dir: Path) -> Path | None:
    """The export a rebuild starts from: conversations.json when it is there,
    otherwise the first recognised export in source/ by name."""
    import build_db
    seed = source_dir / "conversations.json"
    if seed.is_file():
        return seed
    for path in sorted(p for p in source_dir.glob("*.json") if p.is_file()):
        if path.name in ("memories.json", "users.json"):
            continue
        try:
            # Only the first few chats are read (AUDIT.md #31).
            if build_db.export_provider(path):
                return path
        except (OSError, ValueError):
            continue
    return None


def has_rebuild_source(source_dir: Path) -> bool:
    return _rebuild_seed(Path(source_dir)) is not None


def rebuild_history(db_path: Path, source_dir: Path, *, job=None) -> dict:
    """Build history.db from every export in source/ (AUDIT.md #6): the seed
    export first, then every backup that Import New Chats brought in, through
    the same import path. A chat that was permanently deleted (it has a record
    in udb.deleted_records) stays deleted. Renames, Archive, Recycle Bin, pins
    and preferences are in userdata.db and are not touched."""
    import build_db
    db_path, source_dir = Path(db_path), Path(source_dir)
    seed = _rebuild_seed(source_dir)
    if seed is None:
        raise FileNotFoundError(f"No Claude or ChatGPT export found in {source_dir}")
    if job is not None:
        return _rebuild_locked(db_path, source_dir, seed, job)
    return background_jobs.run_sync(db_path, "rebuild",
        lambda task: _rebuild_locked(db_path, source_dir, seed, task))


def _rebuild_locked(db_path: Path, source_dir: Path, seed: Path, job) -> dict:
    import build_db
    build_db.check_free_space(db_path, seed.stat().st_size)
    job.progress("Backing up before the rebuild…", force=True)
    backups.backup_all(db_path, "before-rebuild")
    existing = db_path.exists()
    if not existing:
        # Create a current, empty database before the long scan. A second
        # app can now open even while the first-ever build is counting chats.
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(build_db.SCHEMA)
        finally:
            conn.close()
        _ensure_runtime_schema(db_path)
    fmt = build_db.export_provider(seed)
    if not fmt:
        raise ApiError("No supported conversation export was found in the source folder.", 400)
    scan = build_db.scan_export(seed, fmt, include_bad=True, job=job)
    # Keep the live schema while clearing imported rows in batches.
    if existing:
        _ensure_runtime_schema(db_path)
        conn = open_db(db_path)
        try:
            done = 0
            while True:
                rows = conn.execute("SELECT id FROM conversations LIMIT ?", (background_jobs.BATCH_SIZE,)).fetchall()
                if not rows:
                    break
                job.progress("Preparing the rebuild…", done=done, force=True)
                for row in rows:
                    conversation_tables.delete_imported_rows(conn, row[0])
                    done += 1
                conn.commit()
            for table in ("memories", "project_docs", "projects", "imported_backups", "import_sources"):
                conn.execute(f"DELETE FROM {table}")
                conn.commit()
        finally:
            conn.close()
    build_db.build(seed, db_path, job=job, reset=False, scan=scan)
    _ensure_runtime_schema(db_path)
    conn = open_db(db_path)
    try:
        gone = conn.execute(
            "SELECT c.id FROM conversations c JOIN udb.deleted_records d "
            "ON d.conversation_id = c.id AND d.provider = c.provider"
        ).fetchall()
        for row in gone:
            conversation_tables.delete_imported_rows(conn, row[0])
        # Chats in the Recycle Bin get their newest copy too (they stay there).
        summary = import_new_backups(conn, source_dir, skip_binned=False, job=job)
        conn.commit()
    except background_jobs.Cancelled:
        conn.commit()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    summary["seed"] = seed.name
    # Seed tombstones are excluded before any batch is visible.
    conn = open_db(db_path)
    try:
        seed_ids = set(scan["winners"])
        summary["kept_deleted"] = sum(1 for cid, provider in conn.execute(
            "SELECT conversation_id, provider FROM udb.deleted_records") if cid in seed_ids and provider == fmt)
    finally:
        conn.close()
    return summary


# ── Permanent deletion (Recycle Bin → gone) ─────────────────────────────────────


def _conv_tombstone_meta(conn, cid: str) -> dict | None:
    """Gather the lightweight metadata kept in a permanent-deletion record.

    First/last message dates come from the message rows; the conversation's own
    create/update times are the fallback when a message lacks a timestamp (or a
    metadata-only record has no messages at all). Holds no message content."""
    row = conn.execute(
        f"SELECT c.id, {DISPLAY_TITLE} AS title, "
        "c.provider, c.create_time, c.update_time, c.message_count "
        "FROM conversations c "
        f"{META_JOIN} "
        "WHERE c.id = ?",
        (cid,),
    ).fetchone()
    if row is None:
        return None
    mrow = conn.execute(
        "SELECT MIN(create_time), MAX(create_time), COUNT(*) "
        "FROM messages WHERE conversation_id = ?",
        (cid,),
    ).fetchone()
    msg_first, msg_last, msg_count = (mrow or (None, None, 0))
    first_message = msg_first if msg_first else row["create_time"]
    last_message = msg_last if msg_last else row["update_time"]
    total_messages = msg_count if msg_count else (row["message_count"] or 0)
    return {
        "conversation_id": cid,
        "provider": row["provider"] or "",
        "chat_name": row["title"] or "Untitled",
        "first_message": first_message,
        "last_message": last_message,
        "total_messages": total_messages,
    }


def _purge_conversation(conn, cid: str) -> bool:
    """Permanently remove one conversation: write its deletion record, then drop
    every stored row that belongs to it (content and viewer metadata) from both
    the history and user-data databases, using the one list in
    conversation_tables.py. The caller owns the transaction so a batch stays
    all-or-nothing. Returns True when the conversation existed.

    Never touches the on-disk backup JSON, and never removes shared physical
    attachment/image files (ownership can't be safely established here)."""
    meta = _conv_tombstone_meta(conn, cid)
    if meta is None:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO udb.deleted_records "
        "(conversation_id, provider, chat_name, first_message, last_message, "
        " total_messages, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (meta["conversation_id"], meta["provider"], meta["chat_name"],
         meta["first_message"], meta["last_message"], meta["total_messages"],
         time.time()),
    )
    # Every row that belongs to it, in both databases (conversation_tables.py).
    conversation_tables.delete_conversation_rows(conn, cid, meta["provider"])
    return True


class Handler(BaseHTTPRequestHandler):
    db_path = app_paths.DB_PATH

    source_dir = app_paths.SOURCE_DIR

    def log_message(self, fmt, *args): pass

    def handle_one_request(self):
        t0 = time.perf_counter()
        super().handle_one_request()
        if _TIMING:
            dt = (time.perf_counter() - t0) * 1000
            try:
                sys.stderr.write(
                    f"[timing] {getattr(self, 'command', '?')} "
                    f"{getattr(self, 'path', '?')} {dt:.1f}ms\n"
                )
                sys.stderr.flush()
            except Exception:
                pass

    def _request_target(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        return parsed, path

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_db(self, fn):
        """Run a core logic function that takes an open DB connection, returns a
        JSON-able dict, and raises ApiError for error responses. Opens one
        short-lived connection, sends the result (or the ApiError as a JSON
        error with its status), and always closes the connection. Keeps the API
        handlers thin wrappers around the reusable modules."""
        conn = open_db(self.db_path)
        try:
            self.send_json(fn(conn))
        except ApiError as e:
            self.send_json({"error": e.message}, e.status)
        finally:
            conn.close()

    # POST requests that change many records at once. Both databases are copied
    # before each one runs (backups.py, AUDIT.md #21).
    _BACKUP_BEFORE = {
        "/api/labels/bulk-apply": "before-bulk-change",
        "/api/labels/bulk-runs/continue": "before-bulk-change",
        "/api/recycle-bin/restore": "before-restore",
        "/api/recycle-bin/purge": "before-bulk-change",
    }

    def _backup_reason(self, path):
        if path.startswith("/api/snapshots/") and path.endswith("/restore"):
            return "before-restore"
        return self._BACKUP_BEFORE.get(path)

    def _backup_before(self, reason) -> bool:
        """Copy both databases. If the copy fails, the change is refused."""
        try:
            backups.backup_all(self.db_path, reason)
            return True
        except Exception as exc:
            action, ids = errorlog.describe_path(self.command, self.path)
            errorlog.record_exception(exc, action=action, status=500,
                                      ids={"path": ids} if ids else None)
            self.send_json({"error": "Could not back up the databases first, "
                                     "so nothing was changed. See logs/errors.json."}, 500)
            return False

    def send_response(self, code, message=None):
        self._sent_status = code
        super().send_response(code, message)

    def _log_request_error(self, exc, status=None):
        """Write an error from a request to the error log: the action, the
        status sent (if any), the IDs and the shape of the body, never its
        text."""
        action, path_ids = errorlog.describe_path(getattr(self, "command", "?"),
                                                  getattr(self, "path", ""))
        body = getattr(self, "_logged_body", None)
        ids = {}
        if path_ids:
            ids["path"] = path_ids
        if isinstance(body, dict):
            ids.update(errorlog.ids_in_payload(body))
        errorlog.record_exception(
            exc, action=action,
            status=getattr(self, "_sent_status", None) or status or "no reply sent",
            ids=ids, shape=errorlog.request_shape(body) if body is not None else None,
        )

    def do_POST(self):
        _, path = self._request_target()
        return self._do_post(path)

    def _do_post(self, path):
        reason = self._backup_reason(path)
        if reason and not self._backup_before(reason):
            return
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            self._read_json_body()
            job_id = path[len("/api/jobs/"):-len("/cancel")]
            self.send_json({"job": background_jobs.cancel(self.db_path, job_id)})
        elif path == "/api/rebuild":
            self._read_json_body()
            db, source = Path(self.db_path), Path(self.source_dir)
            if not has_rebuild_source(source):
                raise ApiError("No conversation exports were found in the source folder.", 400)
            def work(job):
                import build_db
                try:
                    return rebuild_history(db, source, job=job)
                except build_db.NotEnoughSpace as exc:
                    raise ApiError(str(exc), 507) from exc
                finally:
                    try:
                        _build_source_index(source)
                    except Exception as exc:
                        errorlog.record_exception(exc, action="Refresh rebuilt files")
            receipt = background_jobs.start(db, "rebuild", work)
            self.send_json({"job": receipt}, 202)
        elif path == "/api/upload-file":
            self._api_upload_file()
        elif path == "/api/error-log/browser":
            self._api_error_log_browser()
        elif path == "/api/pinned":
            self._api_pinned_add()
        elif path == "/api/pinned/reorder":
            self._api_pinned_reorder()
        elif path == "/api/tabs":
            self._api_tabs_create()
        elif path == "/api/folders":
            self._api_folder_create()
        elif path == "/api/folder-items":
            self._api_folder_item_add()
        elif path == "/api/claude-models":
            self._api_claude_model_add()
        elif path == "/api/claude-models/reload":
            self._api_claude_models_reload()
        elif path == "/api/conversation-models":
            self._api_conversation_model_add()
        elif path == "/api/conversation-models/dismiss":
            self._api_conversation_model_dismiss()
        elif path == "/api/tags":
            self._api_tag_add()
        elif path == "/api/mood-tags":
            self._api_mood_tag_add()
        elif path.startswith("/api/gizmos/") and path.endswith("/folder"):
            inner = path[len("/api/gizmos/"):-len("/folder")]
            gid = urllib.parse.unquote(inner.strip("/"))
            payload = self._read_json_body()
            self._handle_db(lambda conn: gizmos.move_to_folder(conn, gid, payload))
        elif path == "/api/labels":
            self._api_label_create()
        elif path == "/api/labels/reorder":
            self._api_labels_reorder()
        elif path == "/api/conversation-labels":
            self._api_conversation_label_set()
        elif path == "/api/labels/bulk-preview-list":
            self._api_bulk_preview_list()
        elif path == "/api/labels/bulk-preview":
            self._api_bulk_preview()
        elif path == "/api/labels/bulk-apply":
            self._api_bulk_apply()
        elif path == "/api/labels/bulk-history/retention":
            self._api_bulk_retention_set()
        elif path == "/api/labels/bulk-runs/continue":
            self._api_bulk_run_continue()
        elif path == "/api/snapshots":
            self._api_snapshot_create()
        elif path.startswith("/api/snapshots/") and path.endswith("/restore"):
            inner = path[len("/api/snapshots/"):-len("/restore")]
            self._api_snapshot_restore(urllib.parse.unquote(inner))
        elif path == "/api/import-new":
            self._api_import_new()
        elif path in ("/api/update/check", "/api/update/apply", "/api/update/restore"):
            self._api_update(path)
        elif path == "/api/recycle-bin/restore":
            self._api_recycle_restore()
        elif path == "/api/recycle-bin/purge":
            self._api_recycle_purge()
        elif path == "/api/import-new/review-import":
            self._api_review_import()
        elif path == "/api/bookmarks":
            self._api_bookmark_create()
        elif path == "/api/message-edits":
            payload = self._read_json_body()
            self._handle_db(lambda conn: message_versions.save_edit(conn, payload))
        elif path == "/api/hidden-messages":
            payload = self._read_json_body()
            self._handle_db(lambda conn: message_versions.set_hidden(conn, payload))
        elif path == "/api/sticky-notes":
            payload = self._read_json_body()
            self._handle_db(lambda conn: message_versions.create_note(conn, payload))
        elif path == "/api/advanced-search":
            payload = self._read_json_body()
            self._handle_db(lambda conn: advanced_search.search(conn, payload))
        elif path == "/api/advanced-search/browse":
            payload = self._read_json_body()
            self._handle_db(lambda conn: advanced_search.browse(conn, payload))
        elif path == "/api/related":
            payload = self._read_json_body()
            self._handle_db(lambda conn: related.link(conn, payload))
        elif path == "/api/advanced-search/saved":
            payload = self._read_json_body()
            self._handle_db(lambda conn: advanced_search.save_search(conn, payload))
        elif path == "/api/scratchpads":
            payload = self._read_json_body()
            self._handle_db(lambda conn: scratchpads.create_pad(conn, payload))
        elif path.startswith("/api/scratchpads/") and path.endswith("/append"):
            pad_id = urllib.parse.unquote(path[len("/api/scratchpads/"):-len("/append")])
            payload = self._read_json_body()
            self._handle_db(lambda conn: scratchpads.append_to_pad(conn, pad_id, payload))
        else:
            self.send_error(404)

    def do_PATCH(self):
        _, path = self._request_target()
        if path == "/api/preferences":
            self._api_preferences_update()
        elif path.startswith("/api/tabs/"):
            self._api_tab_update(urllib.parse.unquote(path[len("/api/tabs/"):]))
        elif path.startswith("/api/folder-items/"):
            self._api_folder_item_update(urllib.parse.unquote(path[len("/api/folder-items/"):]))
        elif path.startswith("/api/folders/"):
            self._api_folder_update(urllib.parse.unquote(path[len("/api/folders/"):]))
        elif path.startswith("/api/conversation/"):
            self._api_conversation_update(urllib.parse.unquote(path[len("/api/conversation/"):]))
        elif path.startswith("/api/claude-models/"):
            self._api_claude_model_update(urllib.parse.unquote(path[len("/api/claude-models/"):]))
        elif path.startswith("/api/gizmos/"):
            gid = urllib.parse.unquote(path[len("/api/gizmos/"):])
            payload = self._read_json_body()
            self._handle_db(lambda conn: gizmos.rename_gizmo(conn, gid, payload))
        elif path.startswith("/api/labels/"):
            self._api_label_update(urllib.parse.unquote(path[len("/api/labels/"):]))
        elif path.startswith("/api/summaries/"):
            self._api_summary_update(urllib.parse.unquote(path[len("/api/summaries/"):]))
        elif path.startswith("/api/notes/"):
            self._api_notes_update(urllib.parse.unquote(path[len("/api/notes/"):]))
        elif path.startswith("/api/bookmarks/"):
            self._api_bookmark_update(urllib.parse.unquote(path[len("/api/bookmarks/"):]))
        elif path.startswith("/api/sticky-notes/"):
            note_id = urllib.parse.unquote(path[len("/api/sticky-notes/"):])
            payload = self._read_json_body()
            self._handle_db(lambda conn: message_versions.update_note(conn, note_id, payload))
        elif path.startswith("/api/scratchpads/"):
            pad_id = urllib.parse.unquote(path[len("/api/scratchpads/"):])
            payload = self._read_json_body()
            self._handle_db(lambda conn: scratchpads.update_pad(conn, pad_id, payload))
        else:
            self.send_error(404)

    def do_DELETE(self):
        _, path = self._request_target()
        if path.startswith("/api/pinned/"):
            self._api_pinned_remove(urllib.parse.unquote(path[len("/api/pinned/"):]))
        elif path.startswith("/api/tabs/"):
            self._api_tab_remove(urllib.parse.unquote(path[len("/api/tabs/"):]))
        elif path.startswith("/api/folder-items/"):
            self._api_folder_item_remove(urllib.parse.unquote(path[len("/api/folder-items/"):]))
        elif path.startswith("/api/claude-models/"):
            self._api_claude_model_remove(urllib.parse.unquote(path[len("/api/claude-models/"):]))
        elif path.startswith("/api/conversation-models/"):
            parts = [urllib.parse.unquote(p) for p
                     in path[len("/api/conversation-models/"):].split("/") if p]
            if len(parts) != 2:
                self.send_error(404); return
            self._api_conversation_model_remove(parts[0], parts[1])
        elif path.startswith("/api/tags/"):
            parts = [urllib.parse.unquote(p) for p
                     in path[len("/api/tags/"):].split("/") if p]
            if len(parts) != 2:
                self.send_error(404); return
            self._api_tag_remove(parts[0], parts[1])
        elif path.startswith("/api/mood-tags/"):
            parts = [urllib.parse.unquote(p) for p
                     in path[len("/api/mood-tags/"):].split("/") if p]
            if len(parts) != 2:
                self.send_error(404); return
            self._api_mood_tag_remove(parts[0], parts[1])
        elif path.startswith("/api/labels/bulk-runs/"):
            self._api_bulk_run_delete(urllib.parse.unquote(path[len("/api/labels/bulk-runs/"):]))
        elif path.startswith("/api/labels/"):
            self._api_label_delete(urllib.parse.unquote(path[len("/api/labels/"):]))
        elif path.startswith("/api/snapshots/"):
            self._api_snapshot_delete(urllib.parse.unquote(path[len("/api/snapshots/"):]))
        elif path.startswith("/api/bookmarks/"):
            self._api_bookmark_delete(urllib.parse.unquote(path[len("/api/bookmarks/"):]))
        elif path.startswith("/api/sticky-notes/"):
            note_id = urllib.parse.unquote(path[len("/api/sticky-notes/"):])
            self._handle_db(lambda conn: message_versions.delete_note(conn, note_id))
        elif path == "/api/related":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            a = (qs.get("a") or [""])[0]
            b = (qs.get("b") or [""])[0]
            self._handle_db(lambda conn: related.unlink(conn, a, b))
        elif path.startswith("/api/advanced-search/saved/"):
            saved_id = urllib.parse.unquote(path[len("/api/advanced-search/saved/"):])
            self._handle_db(lambda conn: advanced_search.delete_saved(conn, saved_id))
        elif path.startswith("/api/scratchpads/"):
            pad_id = urllib.parse.unquote(path[len("/api/scratchpads/"):])
            self._handle_db(lambda conn: scratchpads.delete_pad(conn, pad_id))
        elif path == "/api/error-log":
            errorlog.clear()
            self.send_json({"ok": True})
        else:
            self.send_error(404)

    def _api_error_log_browser(self):
        """Write an error the page sent (AUDIT.md #35). Only same-site pages
        get here (the front-door checks), so no website can fill the log."""
        payload = self._read_json_body()
        self._logged_body = None  # the body describes an error, not a request
        errorlog.record_browser(payload)
        self.send_json({"ok": True})

    def _api_update(self, path):
        """Check for, install, or undo an update (updater.py, AUDIT.md #16).
        Updater failures come back as {"error": "<plain message>"}."""
        if not self._authorized_update_request():
            raise ApiError("This update request did not come from the app's own page.", 403)
        payload = self._read_json_body()
        root = Path(__file__).resolve().parent
        try:
            if path == "/api/update/check":
                result = updater.check_for_update(root)
            elif path == "/api/update/apply":
                result = updater.apply_update(payload.get("version"), payload.get("commit"), root)
            else:
                result = updater.restore_backup(root)
        except updater.UpdateError as exc:
            if exc.status < 500:
                raise ApiError(exc.message, exc.status) from exc
            # Log what went wrong underneath (network, disk), then reply.
            self._log_request_error(exc.__cause__ or exc, exc.status)
            self._reply_error(exc.message, exc.status)
            return
        self.send_json(result)

    def _api_version(self):
        """Report whether the running process is older than the code on disk.

        The page polls this so it can warn when server.py has been replaced but
        the server was not restarted — no version numbers to keep in sync, the
        server just compares its own loaded code to the files on disk."""
        current = _code_signature()
        self.send_json({
            "started_at": _STARTUP_TIME,
            "static_sig": _static_signature(),
            "running_sig": _STARTUP_CODE_SIG,
            "disk_sig": current,
            "stale": current != _STARTUP_CODE_SIG,
            "updating": updater.update_running(Path(__file__).resolve().parent),
        })

    def _content_length(self, limit=request_limits.MAX_BODY) -> int:
        """The request's Content-Length, checked: 400 if it isn't a whole
        number, 413 if it is over `limit`."""
        raw = (self.headers.get("Content-Length") or "0").strip()
        if not raw.isdigit():
            raise ApiError("The request's size header is not a number.", 400)
        length = int(raw)
        if length > limit:
            raise ApiError(f"The request is too large (at most "
                           f"{limit // request_limits.MB} MB).", 413)
        return length

    def _read_json_body(self) -> dict:
        """The request body as a JSON object. An empty body is {}. Anything
        that isn't a JSON object, or a text field over its limit, raises
        ApiError (400 or 413)."""
        length = self._content_length()
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        self._body_read = True
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("The request body is not valid JSON.", 400)
        if not isinstance(parsed, dict):
            raise ApiError("The request body must be a JSON object.", 400)
        # Kept only so an error in this request can log the body's shape.
        self._logged_body = parsed
        request_limits.check_fields(parsed)
        return parsed

    def _discard_unread_body(self):
        """Read and drop a body nobody read (a refused or body-less route), so
        closing the connection doesn't reset it before the browser has read
        the reply. Bodies over MAX_DRAIN are left; the connection just closes."""
        if getattr(self, "_body_read", True):
            return
        self._body_read = True
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        raw = (headers.get("Content-Length") or "0").strip()
        if not raw.isdigit() or not 0 < int(raw) <= request_limits.MAX_DRAIN:
            return
        left = int(raw)
        deadline = time.monotonic() + 5
        try:
            # A caller that stalls mid-body must not hold this thread: each
            # read waits at most 2 s, and the whole drain at most 5 s.
            self.connection.settimeout(2)
            while left > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read(min(left, 1024 * 1024))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None):
        """API addresses get the same JSON error shape as every other API
        error; pages and files keep the standard error page."""
        path = getattr(self, "path", "") or ""
        if not path.startswith("/api/") or getattr(self, "_sent_status", None) is not None:
            return super().send_error(code, message, explain)
        if message is None:
            message = self.responses.get(code, ("Error",))[0]
        self.send_json({"error": message}, code)

    def _qs_int(self, qs, key, default):
        """A whole-number query value; 400 if it isn't one."""
        raw = (qs.get(key) or [str(default)])[0].strip()
        try:
            return int(raw)
        except ValueError:
            raise ApiError(f"{key} must be a whole number.", 400)

    def _reply_error(self, message, status):
        """Send {"error": message}, unless a reply has already started."""
        if self._sent_status is not None:
            return
        try:
            self.send_json({"error": message}, status)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _own_hosts(self) -> tuple[str, ...]:
        port = self.server.server_address[1]
        return (f"127.0.0.1:{port}", f"localhost:{port}")

    def _front_door_problem(self):
        """(status, message) if the request must be refused, else None.

        Host check (AUDIT.md #3): only the app's own address is answered, which
        blocks DNS rebinding. Change check (#1): a POST, PATCH or DELETE must
        come from the app's own page: JSON body (multipart for uploads),
        Origin, when sent, is the app's own address, and Sec-Fetch-Site is not
        cross-site."""
        host = self.headers.get("Host", "").strip().lower()
        if host not in self._own_hosts():
            return 403, "This address is not allowed. Open the app at 127.0.0.1 or localhost."
        if self.command not in ("POST", "PATCH", "PUT", "DELETE"):
            return None
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return 403, "Changes are only accepted from the app's own page."
        origin = self.headers.get("Origin", "").rstrip("/").lower()
        if origin and origin not in tuple(f"http://{h}" for h in self._own_hosts()):
            return 403, "Changes are only accepted from the app's own page."
        _, path = self._request_target()
        has_body = self._content_length(
            request_limits.MAX_UPLOAD if path == "/api/upload-file" else request_limits.MAX_BODY
        ) > 0 or bool(self.headers.get("Transfer-Encoding"))
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if path == "/api/upload-file":
            if content_type != "multipart/form-data":
                return 415, "Uploads must be sent as multipart/form-data."
        elif has_body and content_type != "application/json":
            return 415, "Changes must be sent as JSON."
        return None

    def _authorized_update_request(self) -> bool:
        """Allow the executable-code updater only from this app's own page.

        JSON plus the custom header makes a cross-origin browser request require
        a CORS preflight, which this local server does not grant. Origin and
        Sec-Fetch-Site checks provide a second guard when browsers send them.
        """
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return False
        if self.headers.get("X-CHV-Update") != "1":
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False

        origin = self.headers.get("Origin", "").rstrip("/")
        if origin:
            host = self.headers.get("Host", "")
            if origin not in (f"http://{host}", f"https://{host}"):
                return False
        return True

    def _parse_multipart_form(self, body: bytes, boundary: bytes) -> dict[str, list[dict]]:
        fields: dict[str, list[dict]] = {}
        marker = b"--" + boundary
        for chunk in body.split(marker):
            part = chunk.strip(b"\r\n")
            if not part or part == b"--":
                continue
            if part.endswith(b"--"):
                part = part[:-2]
            if b"\r\n\r\n" not in part:
                continue
            head, data = part.split(b"\r\n\r\n", 1)
            data = data.rstrip(b"\r\n")
            headers = head.decode("utf-8", errors="replace").split("\r\n")
            disp = next((h for h in headers if h.lower().startswith("content-disposition:")), "")
            if not disp:
                continue
            params = dict(re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', disp))
            name = params.get("name", "")
            if not name:
                continue
            fields.setdefault(name, []).append({
                "filename": params.get("filename", ""),
                "data": data,
            })
        return fields

    def _api_upload_file(self):
        length = self._content_length(request_limits.MAX_UPLOAD)
        ctype = self.headers.get("Content-Type", "")
        m = re.match(r"multipart/form-data\s*;\s*boundary=(.+)", ctype, re.IGNORECASE)
        if not m:
            self.send_json({"error": "Expected multipart/form-data"}, 400); return
        boundary = m.group(1).strip().strip('"').encode("utf-8")
        body = self.rfile.read(length)
        self._body_read = True
        fields = self._parse_multipart_form(body, boundary)
        files  = fields.get("file", [])
        names  = fields.get("name", [])
        if not files or not names:
            self.send_json({"error": "Missing file or name"}, 400); return
        file_data = files[0].get("data", b"")
        file_name = names[0].get("data", b"").decode("utf-8", errors="replace")
        # Sanitise: strip path separators
        safe_name = Path(file_name).name
        if not safe_name:
            self.send_json({"error": "Invalid filename"}, 400); return
        check_text("File name", safe_name, request_limits.FILE_NAME_MAX)
        source_dir = Path(self.source_dir)
        dest_dir = source_dir / "files"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe_name
        # Two chats can each have a file with the same name, so an existing
        # file is only replaced once the user has said so (AUDIT.md #24).
        replace = (fields.get("replace") or [{}])[0].get("data", b"") == b"1"
        if dest.exists() and not replace:
            self.send_json({
                "error": f"A file called “{safe_name}” is already saved. It may "
                         "belong to another chat.",
            }, 409)
            return
        dest.write_bytes(file_data)
        # Re-index so next /files/ request can find the new file
        _build_source_index(source_dir)
        self.send_json({"ok": True, "saved": safe_name})

    def do_GET(self):
        parsed, path = self._request_target()
        qs     = urllib.parse.parse_qs(parsed.query)
        static_dir = Path(__file__).parent / "static"

        if path in ("/", "/index.html"):
            self._serve_file(static_dir / "index.html")
        elif path in ("/app.js", "/static/app.js"):
            self._serve_file(static_dir / "app.js")
        elif path in ("/style.css", "/static/style.css"):
            self._serve_file(static_dir / "style.css")
        elif path.startswith("/static/vendor/katex/"):
            self._serve_vendor_file(static_dir / "vendor" / "katex",
                                    path[len("/static/vendor/katex/"):])
        elif path.startswith("/static/") and path.endswith((".js", ".css")):
            # Feature modules split out of app.js (labels_screen.js,
            # bulk_labels.js, snapshots.js, and any future ones) plus split
            # stylesheets (theme.css, settings.css) live in the static dir. Restrict to a
            # flat filename so this can never traverse outside it.
            name = urllib.parse.unquote(path[len("/static/"):])
            if re.fullmatch(r"[\w.-]+\.(?:js|css)", name):
                self._serve_file(static_dir / name)
            else:
                self.send_error(404)
        elif path == "/api/conversations":
            self._api_conversations(qs)
        elif path.startswith("/api/conversation-labels/"):
            self._api_conversation_label_get(
                urllib.parse.unquote(path[len("/api/conversation-labels/"):]))
        elif path.startswith("/api/conversation/"):
            self._api_detail(urllib.parse.unquote(path[len("/api/conversation/"):]), qs)
        elif path == "/api/search":
            self._api_search(qs)
        elif path == "/api/advanced-search/options":
            self._handle_db(advanced_search.options)
        elif path.startswith("/api/related/"):
            self._handle_db(lambda conn: self._related_list(
                conn, urllib.parse.unquote(path[len("/api/related/"):])))
        elif path == "/api/advanced-search/history":
            self._handle_db(advanced_search.history)
        elif path == "/api/scratchpads":
            q = (qs.get("q") or [""])[0]
            self._handle_db(lambda conn: scratchpads.list_pads(conn, q))
        elif path.startswith("/api/scratchpads/"):
            pad_id = urllib.parse.unquote(path[len("/api/scratchpads/"):])
            self._handle_db(lambda conn: scratchpads.get_pad(conn, pad_id))
        elif path == "/api/link-suggestions":
            q = (qs.get("q") or [""])[0]
            self._handle_db(lambda conn: scratchpads.link_suggestions(conn, q))
        elif path == "/api/link-resolve":
            cid = (qs.get("conversation") or [""])[0]
            seq = (qs.get("seq") or [""])[0]
            mid = (qs.get("message") or [""])[0]
            self._handle_db(lambda conn: scratchpads.resolve_link(conn, cid, seq, mid))
        elif path == "/api/gallery":
            self._api_gallery(qs)
        elif path == "/api/attachment-report":
            self._api_attachment_report(qs)
        elif path == "/api/import-audit":
            self._api_import_audit(qs)
        elif path == "/api/imported-backups":
            self._api_imported_backups(qs)
        elif path == "/api/jobs":
            self.send_json({"job": background_jobs.get(self.db_path)})
        elif path.startswith("/api/jobs/"):
            self.send_json({"job": background_jobs.get(self.db_path, path[len("/api/jobs/"):])})
        elif path == "/api/import-new/review":
            self._api_review_list(qs)
        elif path == "/api/folders":
            self._api_folders_list(qs)
        elif path == "/api/memories":
            self._api_memories()
        elif path == "/api/projects":
            self._api_projects()
        elif path.startswith("/api/project/"):
            self._api_project(urllib.parse.unquote(path[len("/api/project/"):]))
        elif path.startswith("/api/artifact/"):
            self._api_artifact(urllib.parse.unquote(path[len("/api/artifact/"):]), qs)
        elif path == "/api/claude-models":
            self._api_claude_models()
        elif path == "/api/conversation-models":
            self._api_conversation_models(qs)
        elif path == "/api/tags":
            self._api_tags_all()
        elif path == "/api/mood-tags":
            self._api_mood_tags_all()
        elif path == "/api/gizmos":
            self._handle_db(gizmos.list_gizmos)
        elif path == "/api/gizmo-conversations":
            gid = ((qs.get("gizmo_id") or [""])[0]).strip()
            self._handle_db(lambda conn: gizmos.list_conversations(conn, gid))
        elif path == "/api/labels":
            self._api_labels_list()
        elif path == "/api/labels/bulk-history":
            self._api_bulk_history()
        elif path == "/api/labels/bulk-runs":
            self._api_bulk_run_list()
        elif path == "/api/snapshots":
            self._api_snapshot_list()
        elif path.startswith("/api/summaries/"):
            self._api_summary_get(urllib.parse.unquote(path[len("/api/summaries/"):]))
        elif path.startswith("/api/notes/"):
            self._api_notes_get(urllib.parse.unquote(path[len("/api/notes/"):]))
        elif path == "/api/global-bookmarks":
            self._api_global_bookmarks(qs)
        elif path == "/api/global-sticky-notes":
            self._api_global_sticky_notes(qs)
        elif path.startswith("/api/bookmarks/"):
            self._api_bookmarks_get(urllib.parse.unquote(path[len("/api/bookmarks/"):]))
        elif path == "/api/version":
            self._api_version()
        elif path == "/api/update/status":
            self.send_json(updater.status(Path(__file__).resolve().parent))
        elif path == "/api/error-log":
            self.send_json({"groups": errorlog.recent_groups(10)})
        elif path == "/api/preferences":
            self._api_preferences()
        elif path == "/api/pinned":
            self._api_pinned_list(qs)
        elif path == "/api/tabs":
            self._api_tabs()
        elif path == "/api/files-manifest":
            self._api_files_manifest()
        elif path == "/api/file-content":
            self._api_file_content(qs)
        elif path == "/api/search-in-conversation":
            self._api_search_in_conversation(qs)
        elif path.startswith("/files/"):
            self._serve_user_file(urllib.parse.unquote(path[len("/files/"):]))
        elif path.startswith("/chatfiles/"):
            self._serve_named_file(urllib.parse.unquote(path[len("/chatfiles/"):]),
                                   Path(__file__).parent / "source" / "chat_files")
        elif path.startswith("/source/"):
            self._serve_source(urllib.parse.unquote(path[len("/source/"):]))
        else:
            self.send_error(404)

    def _serve_file(self, p):
        if not p.is_file():
            self.send_error(404); return
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if mime == "text/html":
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    _VENDOR_TYPES = {
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".woff2": "font/woff2",
    }

    def _serve_vendor_file(self, base: Path, rel: str):
        """Serve a file of a library kept in static/vendor/ (KaTeX, AUDIT.md
        #15): only scripts, stylesheets and fonts, never outside `base`."""
        rel = urllib.parse.unquote(rel)
        parts = rel.split("/")
        ext = Path(rel).suffix.lower()
        if (ext not in self._VENDOR_TYPES
                or not all(re.fullmatch(r"[\w.-]+", p) and p not in (".", "..") for p in parts)):
            self.send_error(404); return
        p = base.joinpath(*parts)
        if not p.is_file():
            self.send_error(404); return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self._VENDOR_TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # File types a user file may be shown as in the browser. Everything else
    # (HTML, SVG, XML, scripts...) is sent as a download, so a file in source/
    # can never run as a page on the app's own address (AUDIT.md #1).
    _INLINE_TYPES = {
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
        "image/avif", "image/x-icon", "image/vnd.microsoft.icon",
        "application/pdf", "text/plain",
        "audio/mpeg", "audio/wav", "audio/ogg", "video/mp4", "video/webm",
    }

    def _send_user_data(self, p: Path, download=False, cache=None):
        """Send a user or export file. Safe types may show in the browser;
        the rest, or all when `download`, are attachments. Never sniffed."""
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        inline = not download and mime in self._INLINE_TYPES
        if mime == "text/plain":
            mime = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cache:
            self.send_header("Cache-Control", cache)
        self.send_header("Content-Disposition",
                         f'{"inline" if inline else "attachment"}; '
                         f'filename="{urllib.parse.quote(p.name)}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_source(self, file_id):
        if not re.match(r'^[\w\-./\u0300-\u036f]+$', file_id):
            self.send_error(400); return
        key = file_id.lstrip("/")
        candidates = _SOURCE_INDEX.get(unicodedata.normalize("NFC", key), [])
        if not candidates:
            self.send_error(404); return
        if len(candidates) > 1:
            self.send_error(409, "Ambiguous source id; use a relative path"); return
        p = candidates[0]
        if p.is_file():
            self._send_user_data(p)
        else:
            self.send_error(404)

    def _serve_named_file(self, filename, base_dir):
        if re.search(r'[\x00-\x1f]', filename) or '..' in filename:
            self.send_error(400); return
        target = (base_dir / filename).resolve()
        if not str(target).startswith(str(base_dir.resolve())):
            self.send_error(403); return
        if not target.is_file():
            self.send_error(404); return
        self._send_user_data(target)

    def _serve_user_file(self, key: str):
        """Serve a file from source/files/ by filename (case-insensitive, space↔underscore)."""
        if re.search(r'[\x00-\x1f]', key) or '..' in key:
            self.send_error(400); return
        files_dir = Path(__file__).parent / "source" / "files"
        p = files_dir / key
        if not p.is_file():
            # Try case-insensitive + space↔underscore normalization
            key_norm = _norm_filename(key)
            p = None
            if files_dir.exists():
                for f in files_dir.iterdir():
                    if _norm_filename(f.name) == key_norm:
                        p = f
                        break
        if not p or not p.is_file():
            self.send_error(404); return
        self._send_user_data(p, download=True, cache="max-age=86400")

    def _api_files_manifest(self):
        """Return list of available files in source/files/ with name→uuid mapping."""
        files_dir = Path(__file__).parent / "source" / "files"
        result = {}
        if files_dir.exists():
            for f in files_dir.iterdir():
                if f.is_file() and not f.name.startswith('.'):
                    result[f.name] = f.name  # name → name (uuid lookup in frontend)
        self.send_json({"files": list(result.keys())})

    def _api_file_content(self, qs):
        """Return text content of a file in source/files/ (DOCX → plain text, image → url)."""
        name = ((qs.get("name") or [""])[0]).strip()
        if not name:
            self.send_json({"error": "missing name"}); return
        files_dir = Path(__file__).parent / "source" / "files"
        # Normalize lookup
        norm = _norm_filename(name)
        real_path = None
        if files_dir.exists():
            for f in files_dir.iterdir():
                if _norm_filename(f.name) == norm:
                    real_path = f
                    break
        if not real_path or not real_path.is_file():
            self.send_json({"error": "This file isn't in the app's files folder."}); return
        ext = real_path.suffix.lower()
        # Image
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif"}
        if ext in image_exts:
            self.send_json({"name": name, "content": "", "type": "image",
                            "url": "/files/" + urllib.parse.quote(real_path.name)}); return
        # DOCX
        if ext == ".docx":
            docx_document = _DocxDocument
            if not _DOCX_OK or not callable(docx_document):
                self.send_json({"error": "Word files can't be previewed because a part of the app is missing. Run the app's setup again."}); return
            try:
                doc = docx_document(str(real_path))
                paragraphs = []
                for para in getattr(doc, "paragraphs", []):
                    text = getattr(para, "text", "")
                    if text.strip():
                        paragraphs.append(text)
                content = "\n\n".join(paragraphs)
                self.send_json({"name": name, "content": content, "type": "docx"}); return
            except Exception as e:
                self._log_request_error(e)
                self.send_json({"error": "This Word file couldn't be opened."}); return
        # PDF or other text-like file: try reading as UTF-8 text
        if ext == ".pdf":
            self.send_json({"error": "PDFs can't be previewed here. Download the file to open it."}); return
        try:
            content = real_path.read_text(encoding="utf-8", errors="replace")
            self.send_json({"name": name, "content": content, "type": ext.lstrip(".")}); return
        except Exception as e:
            self._log_request_error(e)
            self.send_json({"error": "This file couldn't be opened."}); return

    def _api_search_in_conversation(self, qs):
        conv_id = ((qs.get("conv_id") or [""])[0]).strip()
        q = ((qs.get("q") or [""])[0]).strip()
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if not conv_id or not q:
            self.send_json({"matches": []}); return
        conn = open_db(self.db_path)
        try:
            # Messages whose content contains the term
            content_rows = conn.execute(
                "SELECT seq, role FROM messages WHERE conversation_id = ? AND content LIKE ? ESCAPE '\\' ORDER BY seq",
                (conv_id, pattern)
            ).fetchall()

            # Messages linked to artifacts whose content contains the term
            try:
                artifact_rows = conn.execute("""
                    SELECT DISTINCT m.seq, m.role
                    FROM messages m
                    JOIN json_each(m.artifact_ids) j ON json_valid(m.artifact_ids)
                    JOIN artifacts a ON a.conv_id = m.conversation_id AND a.id = j.value
                    WHERE m.conversation_id = ?
                      AND a.content LIKE ? ESCAPE '\\'
                    ORDER BY m.seq
                """, (conv_id, pattern)).fetchall()
            except Exception as exc:
                self._log_request_error(exc)
                raise

            # Merge and deduplicate by seq, preserving order
            seen = {}
            for r in list(content_rows) + list(artifact_rows):
                if r["seq"] not in seen:
                    seen[r["seq"]] = r["role"]
            matches = [{"seq": k, "role": v} for k, v in sorted(seen.items())]
            self.send_json({"matches": matches, "total": len(matches)})
        finally:
            conn.close()

    def _send_conv_list(self, conn, rows, total, offset, limit, provider=None):
        """Ship a conversation page, tagged with each row's warning icons.

        unverified_count drives the "Unverified Models" entry in the sidebar
        filter, which only exists while something is actually flagged. Model
        warnings are Claude-only, so they are computed only when the Claude side
        is being shown (or, for an older client that sends no provider, when the
        whole dataset is Claude).
        """
        convs = [dict(r) for r in rows]
        # Attach each row's labels in one batched query so the sidebar can draw
        # its squares without a request per conversation (no N+1).
        by_conv = labels.labels_by_conv(conn, [c["id"] for c in convs])
        for c in convs:
            c["labels"] = by_conv.get(c["id"], [])
        # Tag rows that have a non-empty condensed summary so the sidebar hint
        # knows whether to show the hoverable "Summary" link.
        cids = [c["id"] for c in convs]
        has_summary = set()
        if cids:
            marks = ",".join("?" * len(cids))
            for r in conn.execute(
                "SELECT conversation_id FROM udb.conversation_summaries "
                f"WHERE conversation_id IN ({marks}) "
                "AND condensed_summary IS NOT NULL "
                "AND TRIM(condensed_summary) != ''", tuple(cids),
            ).fetchall():
                has_summary.add(r["conversation_id"])
        for c in convs:
            c["has_condensed_summary"] = c["id"] in has_summary
        claude_side = (provider == "claude") if provider else (self._dataset_format(conn) == "claude")
        if claude_side:
            flags = _compute_conv_flags(
                conn, [(c["id"], c.get("create_time"), c.get("update_time")) for c in convs]
            )
            for c in convs:
                f = flags.get(c["id"])
                c["warn_range"] = bool(f and f["warn_range"])
                c["warn_coverage"] = bool(f and f["warn_coverage"])
            unverified = len(_flagged_conv_ids(conn))
        else:
            unverified = 0
        self.send_json({"conversations": convs, "total": total, "offset": offset,
                        "limit": limit, "unverified_count": unverified})

    def _related_list(self, conn, cid):
        """Related Conversations for one chat, as sidebar rows (labels, summary
        hint and, for Claude chats, the model warning icons) plus location."""
        rows = related.list_related(conn, cid)
        claude_rows = [r for r in rows if r.get("provider") == "claude"]
        flags = _compute_conv_flags(
            conn, [(r["id"], r.get("create_time"), r.get("update_time")) for r in claude_rows]
        )
        for r in rows:
            f = flags.get(r["id"])
            r["warn_range"] = bool(f and f["warn_range"])
            r["warn_coverage"] = bool(f and f["warn_coverage"])
        return {"related": rows}

    def _api_conversations(self, qs):
        limit  = min(500, max(1, self._qs_int(qs, "limit", 50)))
        offset = max(0, self._qs_int(qs, "offset", 0))
        q      = ((qs.get("q") or [""])[0]).strip()
        view   = ((qs.get("view") or ["recent"])[0]).strip().lower()
        pinned_first = ((qs.get("pinned_first") or ["0"])[0]).strip() in ("1", "true", "yes")
        # Which side (Claude or ChatGPT) the sidebar toggle is showing.
        provider = query_rules.provider_from_qs(qs)
        prov_sql = query_rules.and_provider(provider)
        # A "label:<id>" (or "label:__unlabeled__") view filters by conversation
        # label. It behaves like the "all" view for the deleted/archived/folder
        # gating, then adds the label constraint — so a labelled chat still shows
        # even when archived or inside a folder.
        label_filter = None
        if view.startswith("label:"):
            label_filter = view[len("label:"):]
            view = "labelview"
        conn = open_db(self.db_path)
        try:
            if view == "unverified":
                flagged = _flagged_conv_ids(conn)
                if not flagged:
                    self.send_json({"conversations": [], "total": 0, "offset": offset,
                                    "limit": limit, "unverified_count": 0})
                    return
                marks = ",".join("?" * len(flagged))
                rows = conn.execute(
                    f"SELECT c.id, {DISPLAY_TITLE} AS title, "
                    "c.create_time, c.update_time, c.message_count, c.preview "
                    "FROM conversations c "
                    f"{META_JOIN} "
                    f"WHERE c.id IN ({marks}) AND {NOT_DELETED}" + prov_sql + " "
                    "ORDER BY c.update_time DESC, c.create_time DESC LIMIT ? OFFSET ?",
                    (*flagged, limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM conversations c "
                    f"{META_JOIN} "
                    f"WHERE c.id IN ({marks}) AND {NOT_DELETED}" + prov_sql,
                    tuple(flagged),
                ).fetchone()[0]
                self._send_conv_list(conn, rows, total, offset, limit, provider)
                return

            if view == "pinned":
                rows = conn.execute(
                    f"SELECT c.id, {DISPLAY_TITLE} AS title, c.create_time, c.update_time, c.message_count, c.preview "
                    "FROM udb.pinned_conversations p "
                    "JOIN conversations c ON c.id = p.conversation_id "
                    f"{META_JOIN} "
                    f"WHERE {NOT_DELETED} "
                    # Foldered chats live only in their folder, never the loose
                    # Pinned view.
                    "AND c.id NOT IN (SELECT conversation_id FROM udb.folder_items)"
                    + prov_sql + " "
                    # Ordered newest-first like the other views so the list's
                    # month headers stay chronological. (The pinned view is now
                    # a filter, not the old drag-to-reorder top section.)
                    "ORDER BY c.update_time DESC, c.create_time DESC "
                    "LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM udb.pinned_conversations p "
                    "JOIN conversations c ON c.id = p.conversation_id "
                    f"{query_rules.meta_join('p.conversation_id')} "
                    f"WHERE {NOT_DELETED} "
                    "AND p.conversation_id NOT IN (SELECT conversation_id FROM udb.folder_items)"
                    + prov_sql
                ).fetchone()[0]
                self._send_conv_list(conn, rows, total, offset, limit, provider)
                return

            where_clauses = []
            if view == "deleted":
                where_clauses.append(IN_RECYCLE_BIN)
            else:
                where_clauses.append(NOT_DELETED)

            if view == "archived":
                where_clauses.append(query_rules.archived())
            elif view in ("all", "deleted", "labelview"):
                pass
            else:
                where_clauses.append(query_rules.not_archived())

            if view == "recent":
                where_clauses.append(
                    "c.id NOT IN (SELECT conversation_id FROM udb.pinned_conversations)"
                )
            # Foldered chats never appear in the main list — they live only in
            # their folder in the sidebar — EXCEPT in the Recycle Bin. A deleted
            # chat is hidden from its folder (the folder query filters
            # deleted = 0), so the Recycle Bin is the only place it can be seen
            # and restored; excluding it here would strand it. Its folder
            # membership is preserved so restoring returns it to that folder.
            # A label view searches across folders too, so a labelled chat that
            # lives in a folder still appears under its label filter.
            if view not in ("deleted", "labelview"):
                where_clauses.append(
                    "c.id NOT IN (SELECT conversation_id FROM udb.folder_items)"
                )
            # Restrict to the toggled side (Claude or ChatGPT).
            if provider:
                where_clauses.append(query_rules.is_provider(provider))
            # The label constraint. label_id is a uuid4 hex, validated before it
            # is interpolated; an unrecognised value yields no
            # rows rather than an error.
            if label_filter is not None:
                if label_filter == "__unlabeled__":
                    where_clauses.append(
                        "c.id NOT IN (SELECT conversation_id FROM udb.conversation_labels)"
                    )
                elif re.fullmatch(r"[0-9a-f]{32}", label_filter):
                    where_clauses.append(
                        "c.id IN (SELECT conversation_id FROM udb.conversation_labels "
                        f"WHERE label_id = '{label_filter}')"
                    )
                else:
                    where_clauses.append("0")
            where_sql = " AND ".join(where_clauses)

            # One query for every view; only the search part differs. A search
            # of 3+ characters uses the full-text index plus the display title
            # and tags; a shorter one, or one the index can't read, matches the
            # display title and tags only.
            def search_sql(use_index):
                if not q:
                    return "", ()
                like = f"%{q}%"
                if use_index:
                    return (
                        " AND c.id IN ("
                        "  SELECT conversation_id FROM search_index WHERE search_index MATCH ? "
                        "  UNION "
                        "  SELECT c2.id FROM conversations c2 "
                        f"  {query_rules.meta_join('c2.id', 'cm2')} "
                        f"  WHERE {query_rules.not_deleted('cm2')} "
                        f"  AND {query_rules.display_title('c2', 'cm2')} LIKE ?"
                        "  UNION "
                        "  SELECT conversation_id FROM udb.conversation_tags WHERE tag LIKE ?"
                        "  UNION "
                        "  SELECT conversation_id FROM udb.conversation_mood_tags WHERE tag LIKE ?"
                        ")", (q, like, like, like))
                return (
                    f" AND ({DISPLAY_TITLE} LIKE ? "
                    "OR c.id IN (SELECT conversation_id FROM udb.conversation_tags WHERE tag LIKE ?) "
                    "OR c.id IN (SELECT conversation_id FROM udb.conversation_mood_tags WHERE tag LIKE ?))",
                    (like, like, like))

            order_sql = (
                "ORDER BY CASE WHEN p.conversation_id IS NULL THEN 1 ELSE 0 END, "
                "p.order_index ASC, c.update_time DESC, c.create_time DESC"
            ) if pinned_first else "ORDER BY c.update_time DESC, c.create_time DESC"

            def run(use_index):
                match_sql, match_params = search_sql(use_index)
                rows = conn.execute(
                    f"SELECT c.id, {DISPLAY_TITLE} AS title, c.create_time, c.update_time, c.message_count, c.preview "
                    "FROM conversations c "
                    f"{META_JOIN} "
                    "LEFT JOIN udb.pinned_conversations p ON p.conversation_id = c.id "
                    "WHERE " + where_sql + match_sql + " "
                    + order_sql + " LIMIT ? OFFSET ?",
                    (*match_params, limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM conversations c "
                    f"{META_JOIN} "
                    "WHERE " + where_sql + match_sql,
                    match_params,
                ).fetchone()[0]
                return rows, total

            if q and len(q) >= 3:
                try:
                    rows, total = run(True)
                except sqlite3.Error:
                    rows, total = run(False)
            else:
                rows, total = run(False)
            self._send_conv_list(conn, rows, total, offset, limit, provider)
        finally:
            conn.close()

    def _api_detail(self, conv_id, qs=None):
        qs = qs or {}
        limit = min(200, max(1, self._qs_int(qs, "limit", 100)))
        offset = max(0, self._qs_int(qs, "offset", 0))
        conn = open_db(self.db_path)
        try:
            conv = conn.execute(
                f"SELECT c.id, {DISPLAY_TITLE} AS title, "
                "c.create_time, c.update_time, c.message_count, c.preview, "
                "COALESCE(c.import_status, 'normal') AS import_status, "
                "c.provider AS provider, c.gizmo_id, c.gizmo_type, "
                "COALESCE(gn.display_name, '') AS gizmo_name, "
                "COALESCE(cm.deleted, 0) AS deleted, "
                # The conversation's own pin/folder state, so the thread menu is
                # correct even for a Compare item from the opposite provider
                # (whose state is absent from the sidebar-scoped lists).
                "CASE WHEN pc.conversation_id IS NULL THEN 0 ELSE 1 END AS pinned, "
                "fi.folder_id AS folder_id, "
                "COALESCE(fi.pinned, 0) AS folder_pinned "
                "FROM conversations c "
                f"{META_JOIN} "
                "LEFT JOIN udb.pinned_conversations pc ON pc.conversation_id = c.id "
                "LEFT JOIN udb.folder_items fi ON fi.conversation_id = c.id "
                "LEFT JOIN udb.gizmo_names gn ON gn.gizmo_id = c.gizmo_id "
                # No deleted filter: soft-deleted means "kept out of the lists",
                # not "unreadable". The import audit lists these deliberately,
                # and refusing them here left it opening records it could not show.
                "WHERE c.id = ?", (conv_id,)
            ).fetchone()
            if not conv:
                self.send_error(404); return
            total = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)).fetchone()[0]
            position = offset
            if "target_seq" in qs:
                target = self._qs_int(qs, "target_seq", 0)
                position = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND seq < ?", (conv_id, target)).fetchone()[0]
            page_index = min(position // limit, max(0, (total - 1) // limit))

            def boundary(index):
                """Canonical page edge, shifted past a user/response pair.

                Computing both edges from nominal page numbers makes Previous
                the exact inverse of Next, including pages extended by one.
                Only two roles are read at each edge; message bodies stay paged.
                """
                edge = min(total, max(0, index * limit))
                if edge and edge < total:
                    roles = conn.execute(
                        "SELECT role FROM messages WHERE conversation_id = ? ORDER BY seq LIMIT 2 OFFSET ?",
                        (conv_id, edge - 1),
                    ).fetchall()
                    if len(roles) == 2 and roles[0][0] == "user" and roles[1][0] == "assistant":
                        edge += 1
                return edge

            offset = boundary(page_index)
            # A target response belongs with its user on the preceding page.
            # limit=1 can also leave an empty nominal page between two pairs.
            while page_index and (offset >= total or position < offset):
                page_index -= 1
                offset = boundary(page_index)
            end = boundary(page_index + 1)
            previous_index = page_index - 1
            previous_offset = boundary(previous_index) if previous_index >= 0 else None
            while previous_index > 0 and previous_offset >= offset:
                previous_index -= 1
                previous_offset = boundary(previous_index)
            page_limit = end - offset
            msgs = conn.execute(
                "SELECT seq, role, content, attachments, artifact_ids, siblings, branch_index, create_time, "
                "message_uuid FROM messages WHERE conversation_id = ? ORDER BY seq LIMIT ? OFFSET ?", (conv_id, page_limit, offset),
            ).fetchall()
            def parse_msg(m):
                d = dict(m)
                for field in ("attachments", "siblings", "artifact_ids"):
                    raw = d.get(field)
                    if raw:
                        try: d[field] = json.loads(raw)
                        except: d[field] = [] if field != "siblings" else None
                    else:
                        d[field] = [] if field != "siblings" else None
                return d
            try:
                art_rows = conn.execute(
                    "SELECT id, conv_id, title, type, lang FROM artifacts WHERE conv_id = ?", (conv_id,),
                ).fetchall()
                artifacts_meta = {r["id"]: dict(r) for r in art_rows}
            except Exception:
                artifacts_meta = {}
            # The model strip's state rides along with the detail so opening a
            # conversation stays a single request. None on a ChatGPT export,
            # where the whole feature is absent.
            models = (self._conv_model_state(conn, conv_id)
                      if self._dataset_format(conn) == "claude" else None)
            conv_dict = dict(conv)
            if conv_dict.get("gizmo_id"):
                conv_dict["gizmo_conversation_count"] = conn.execute(
                    "SELECT COUNT(*) FROM conversations c "
                    f"{META_JOIN} "
                    f"WHERE {query_rules.is_provider('chatgpt')} AND c.gizmo_id = ? "
                    f"AND {NOT_DELETED}",
                    (conv_dict["gizmo_id"],),
                ).fetchone()[0]
            else:
                conv_dict["gizmo_conversation_count"] = 0
            # The current label rides along so opening a conversation stays one
            # request; the header square reads it directly.
            conv_dict["labels"] = labels.conv_labels(conn, conv_id)
            self.send_json({"conversation": conv_dict,
                            "messages": [parse_msg(m) for m in msgs],
                            "offset": offset, "limit": limit, "total": total,
                            "previous_offset": previous_offset,
                            "next_offset": end if end < total else None,
                            "artifacts": artifacts_meta,
                            "models": models,
                            "tags": self._conv_tags(conn, conv_id),
                            "mood_tags": self._conv_mood_tags(conn, conv_id),
                            "bookmarks": self._conv_bookmarks(conn, conv_id),
                            **message_versions.conversation_extras(conn, conv_id)})
        finally:
            conn.close()

    def _api_search(self, qs):
        q     = ((qs.get("q") or [""])[0]).strip()
        limit = min(500, max(1, self._qs_int(qs, "limit", 40)))
        # Same rules as the sidebar: never the Recycle Bin, only the side the
        # sidebar shows (both when none is given), and the user's rename.
        provider = query_rules.provider_from_qs(qs)
        if not q:
            self.send_json({"results": [], "q": q}); return
        conn = open_db(self.db_path)
        try:
            listed = (
                "SELECT c.id FROM conversations c "
                f"{META_JOIN} "
                f"WHERE {NOT_DELETED}" + query_rules.and_provider(provider)
            )
            # Step 1: find matching conversations via FTS or title LIKE
            conv_ids = []
            conv_ids_set = set()

            def add(sql, params):
                for r in conn.execute(sql, params).fetchall():
                    if r[0] not in conv_ids_set:
                        conv_ids.append(r[0]); conv_ids_set.add(r[0])

            # Title matches come first: the full-text index holds the imported
            # title only, so a chat found by its new name must not be pushed
            # past the cut below by index matches.
            add("SELECT c.id FROM conversations c "
                f"{META_JOIN} "
                f"WHERE {DISPLAY_TITLE} LIKE ? AND c.id IN ({listed}) LIMIT 15", (f"%{q}%",))
            if len(q) >= 3:
                try:
                    add("SELECT DISTINCT conversation_id FROM search_index WHERE search_index MATCH ? "
                        f"AND conversation_id IN ({listed}) LIMIT 30", (q,))
                except sqlite3.Error:
                    pass
            # And tag matches (ordinary tags and mood tags both count)
            add("SELECT DISTINCT conversation_id FROM udb.conversation_tags WHERE tag LIKE ? "
                f"AND conversation_id IN ({listed}) LIMIT 15", (f"%{q}%",))
            add("SELECT DISTINCT conversation_id FROM udb.conversation_mood_tags WHERE tag LIKE ? "
                f"AND conversation_id IN ({listed}) LIMIT 15", (f"%{q}%",))

            # Step 2: for each conv, find messages matching query
            results = []
            for conv_id in conv_ids[:25]:
                conv = conn.execute(
                    f"SELECT {DISPLAY_TITLE} AS title FROM conversations c {META_JOIN} WHERE c.id = ?",
                    (conv_id,)
                ).fetchone()
                if not conv: continue
                conv_title = conv["title"]
                msg_rows = conn.execute(
                    "SELECT seq, role, content FROM messages "
                    "WHERE conversation_id = ? AND content LIKE ? ORDER BY seq LIMIT 3",
                    (conv_id, f"%{q}%")
                ).fetchall()
                if msg_rows:
                    for m in msg_rows:
                        snippet = _extract_snippet(m["content"] or "", q, 100)
                        results.append({
                            "conv_id":    conv_id,
                            "conv_title": conv_title,
                            "seq":        m["seq"],
                            "role":       m["role"],
                            "snippet":    snippet,
                        })
                else:
                    # Title matched but no message body match → add title-level result
                    results.append({
                        "conv_id":    conv_id,
                        "conv_title": conv_title,
                        "seq":        None,
                        "role":       None,
                        "snippet":    "",
                    })
            self.send_json({"results": results[:limit], "q": q})
        finally:
            conn.close()

    def _api_gallery(self, qs=None):
        qs = qs or {}
        limit = min(200, max(1, self._qs_int(qs, "limit", 100)))
        offset = max(0, self._qs_int(qs, "offset", 0))
        provider = query_rules.provider_from_qs(qs)
        scope = query_rules.and_provider(provider)

        image_exts = frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp", "svg", "heic", "heif"})

        def is_image_like(name: str, file_type: str) -> bool:
            file_type = (file_type or "").strip().lower().lstrip(".")
            name_ext = Path(name or "").suffix.lower().lstrip(".")
            return file_type == "image" or file_type in image_exts or name_ext in image_exts

        def ensure_group(groups: dict, conv_id: str, title: str, update_time, message_count, preview: str):
            group = groups.get(conv_id)
            if group is None:
                group = {
                    "conv_id": conv_id,
                    "conv_title": title,
                    "update_time": update_time,
                    "message_count": message_count,
                    "preview": preview or "",
                    "items": [],
                }
                groups[conv_id] = group
            return group

        def finalize_groups(groups: dict) -> list[dict]:
            out = list(groups.values())
            for group in out:
                group["items"].sort(
                    key=lambda item: (
                        item.get("seq") is None,
                        item.get("seq") or 0,
                        item.get("name") or "",
                    )
                )
            out.sort(key=lambda group: group.get("update_time") or 0, reverse=True)
            return out

        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT m.conversation_id AS conv_id, m.seq, m.role, substr(m.content, 1, 120) AS content, j.value AS attachment, "
                "c.title AS conv_title, c.update_time, c.message_count, c.preview, "
                f"{DISPLAY_TITLE} AS display_title "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                f"{META_JOIN} "
                "JOIN json_each(CASE WHEN json_valid(m.attachments) THEN m.attachments ELSE '[]' END) j "
                f"WHERE {NOT_DELETED}" + scope + " AND j.type = 'object' "
                "ORDER BY c.update_time DESC, c.id, m.seq, j.key LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()

            image_groups = {}
            attachment_groups = {}
            linked_image_names = set()

            for r in rows:
                conv_id = r["conv_id"]
                title = r["display_title"] or r["conv_title"] or conv_id
                base_item = {
                    "conv_id": conv_id,
                    "conv_title": title,
                    "update_time": r["update_time"],
                    "message_count": r["message_count"],
                    "preview": r["preview"] or "",
                    "seq": r["seq"],
                    "role": r["role"],
                    "context": (r["content"] or "").strip()[:120],
                }
                try:
                    attachments = [json.loads(r["attachment"])]
                except Exception:
                    attachments = []
                for att in attachments:
                    if not isinstance(att, dict):
                        continue
                    name = (att.get("name") or att.get("file_name") or "").strip()
                    file_type = (att.get("type") or att.get("file_type") or "").strip()
                    content = (att.get("content") or "").strip()
                    item = {
                        **base_item,
                        "name": name or "Attachment",
                        "type": file_type,
                        "has_content": bool(content),
                    }
                    if is_image_like(name, file_type):
                        item["kind"] = "image"
                        linked_image_names.add(_norm_filename(name or item["name"]))
                        ensure_group(image_groups, conv_id, title, r["update_time"], r["message_count"], r["preview"])["items"].append(item)
                    else:
                        item["kind"] = "attachment"
                        ensure_group(attachment_groups, conv_id, title, r["update_time"], r["message_count"], r["preview"])["items"].append(item)

            artifact_groups = {}
            art_rows = conn.execute(
                "SELECT a.id, a.conv_id, a.msg_seq, a.title, a.type, a.lang, substr(a.content, 1, 120) AS content, "
                "c.title AS conv_title, c.update_time, c.message_count, c.preview, "
                f"{DISPLAY_TITLE} AS display_title "
                "FROM artifacts a JOIN conversations c ON c.id = a.conv_id "
                f"{META_JOIN} "
                f"WHERE {NOT_DELETED}" + scope + " "
                "ORDER BY c.update_time DESC, c.id, a.msg_seq, a.id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            for r in art_rows:
                title = r["display_title"] or r["conv_title"] or r["conv_id"]
                ensure_group(artifact_groups, r["conv_id"], title, r["update_time"], r["message_count"], r["preview"])["items"].append({
                    "conv_id": r["conv_id"],
                    "conv_title": title,
                    "update_time": r["update_time"],
                    "message_count": r["message_count"],
                    "preview": r["preview"] or "",
                    "artifact_id": r["id"],
                    "msg_seq": r["msg_seq"],
                    "seq": r["msg_seq"],
                    "name": r["title"] or r["id"],
                    "type": r["type"] or "artifact",
                    "lang": r["lang"] or "",
                    "context": (r["content"] or "").strip()[:120],
                    "kind": "artifact",
                })

            linked_image_names = {
                _norm_filename(r[0]) for r in conn.execute(
                    "SELECT COALESCE(json_extract(j.value, '$.name'), json_extract(j.value, '$.file_name'), '') "
                    "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    f"{META_JOIN} JOIN json_each(CASE WHEN json_valid(m.attachments) THEN m.attachments ELSE '[]' END) j "
                    f"WHERE {NOT_DELETED} AND j.type = 'object'")
            }
            unlinked_images = []
            source_dirs = [
                Path(self.source_dir) / "dalle-generations",
                Path(self.source_dir) / "files",
            ]
            for src_dir in source_dirs:
                if not src_dir.exists():
                    continue
                for f in sorted(src_dir.iterdir()):
                    if not f.is_file() or f.name.startswith('.'):
                        continue
                    if f.suffix.lower().lstrip(".") not in image_exts:
                        continue
                    if src_dir.name == "files" and _norm_filename(f.name) in linked_image_names:
                        continue
                    unlinked_images.append({
                        "filename": f.name,
                        "url": f"/source/{f.name}" if src_dir.name == "dalle-generations" else "/files/" + urllib.parse.quote(f.name),
                    })

            self.send_json({
                "sections": [
                    {
                        "key": "images",
                        "title": "Conversation Images",
                        "meta": "Images that can jump back to their source conversation.",
                        "count": sum(len(group["items"]) for group in image_groups.values()),
                        "groups": finalize_groups(image_groups),
                    },
                    {
                        "key": "attachments",
                        "title": "Attachments",
                        "meta": "All attachment chips grouped by conversation.",
                        "count": sum(len(group["items"]) for group in attachment_groups.values()),
                        "groups": finalize_groups(attachment_groups),
                    },
                    {
                        "key": "artifacts",
                        "title": "Artifacts",
                        "meta": "Claude-generated artifacts grouped by the conversation that created them.",
                        "count": sum(len(group["items"]) for group in artifact_groups.values()),
                        "groups": finalize_groups(artifact_groups),
                    },
                ],
                "unlinked_images": unlinked_images[offset:offset + limit],
                "unlinked_total": len(unlinked_images),
                "offset": offset, "limit": limit,
                "next_offset": offset + limit if (len(rows) == limit or len(art_rows) == limit or len(unlinked_images) > offset + limit) else None,
            })
        finally:
            conn.close()

    def _api_attachment_report(self, qs=None):
        """Return all attachments that couldn't be displayed (no content + not in
        source/files/), from the conversations the sidebar lists: never the
        Recycle Bin, only the given side, under the user's rename."""
        provider = query_rules.provider_from_qs(qs or {})
        files_dir = Path(__file__).parent / "source" / "files"
        local_norms = set()
        if files_dir.exists():
            for f in files_dir.iterdir():
                if f.is_file() and not f.name.startswith('.'):
                    local_norms.add(_norm_filename(f.name))
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT m.seq, m.role, m.content, m.attachments, c.id AS conv_id, {DISPLAY_TITLE} AS conv_title "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                f"{META_JOIN} "
                "WHERE m.attachments IS NOT NULL AND m.attachments != '[]' AND m.attachments != '' "
                f"AND {NOT_DELETED}" + query_rules.and_provider(provider) + " "
                "ORDER BY c.update_time DESC, m.seq"
            ).fetchall()
        finally:
            conn.close()

        missing = []
        seen = set()
        for r in rows:
            try:
                atts = json.loads(r["attachments"] or "[]")
            except Exception:
                continue
            for a in atts:
                name = a.get("name") or ""
                if not name:
                    continue
                has_content = bool(a.get("content"))
                norm = _norm_filename(name)
                in_local = norm in local_norms
                if not has_content and not in_local:
                    key = (r["conv_id"], name)
                    if key in seen:
                        continue
                    seen.add(key)
                    ctx = (r["content"] or "").strip()[:80]
                    missing.append({
                        "conv_id":    r["conv_id"],
                        "conv_title": r["conv_title"],
                        "seq":        r["seq"],
                        "role":       r["role"],
                        "file_name":  name,
                        "file_type":  a.get("type") or "",
                        "context":    ctx,
                    })
        self.send_json({"missing": missing, "total": len(missing)})

    def _api_import_audit(self, qs):
        """Import-audit inspection view.

        Without ?status=… : return per-status counts over the whole database.
        With    ?status=X : return the conversations whose import_status = X,
        so the UI can browse the records that came in as metadata_only, etc.
        """
        status = ((qs.get("status") or [""])[0]).strip().lower()
        valid = {"normal", "fallback", "metadata_only", "parse_error"}
        conn = open_db(self.db_path)
        try:
            # import_status/source_index may be absent on very old DBs; the
            # runtime schema adds them, but stay defensive.
            try:
                # import_sources holds one row per object in conversations.json,
                # duplicates included, so the audit adds up to the source file.
                # Databases built before it existed fall back to conversations,
                # which undercounts collapsed duplicates but still works.
                by_source = False
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_sources'"
                ).fetchone():
                    # message_count arrived with the per-source metadata; an
                    # import_sources built before it can't answer these queries.
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(import_sources)")}
                    by_source = "message_count" in cols and bool(
                        conn.execute("SELECT 1 FROM import_sources LIMIT 1").fetchone()
                    )

                if status:
                    if status not in valid:
                        self.send_json({"error": "unknown status"}, 400); return
                    if by_source:
                        # Every column comes from the source object itself. A
                        # collapsed duplicate reporting the winning record's
                        # message count and dates would contradict the very
                        # thing this view exists to show. Only a user's rename
                        # is borrowed, and only for the record actually stored.
                        rows = conn.execute(
                            "SELECT s.conversation_id AS id, "
                            "CASE WHEN s.kept = 1 "
                            f"     THEN {query_rules.display_title('s')} "
                            "     ELSE s.title END AS title, "
                            "s.create_time, s.update_time, "
                            "COALESCE(s.message_count, 0) AS message_count, s.preview, "
                            "s.import_status, s.source_index, s.kept "
                            "FROM import_sources s "
                            f"{query_rules.meta_join('s.conversation_id')} "
                            "WHERE s.import_status = ? "
                            "ORDER BY s.source_index ASC",
                            (status,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT c.id, "
                            f"{DISPLAY_TITLE} AS title, "
                            "c.create_time, c.update_time, c.message_count, c.preview, "
                            "c.import_status, c.source_index, 1 AS kept "
                            "FROM conversations c "
                            f"{META_JOIN} "
                            # No archived/deleted filter: this is an import audit, so
                            # it must show every record of the status (counts match).
                            "WHERE COALESCE(c.import_status, 'normal') = ? "
                            "ORDER BY c.source_index IS NULL, c.source_index ASC, c.create_time DESC",
                            (status,),
                        ).fetchall()
                    self.send_json({
                        "status": status,
                        "conversations": [dict(r) for r in rows],
                        "total": len(rows),
                    })
                    return

                counts = {}
                if by_source:
                    for r in conn.execute(
                        "SELECT import_status AS s, COUNT(*) AS n "
                        "FROM import_sources GROUP BY import_status"
                    ).fetchall():
                        counts[r["s"]] = r["n"]
                    total = conn.execute("SELECT COUNT(*) FROM import_sources").fetchone()[0]
                    synthetic = conn.execute(
                        "SELECT COUNT(*) FROM import_sources WHERE synthetic = 1"
                    ).fetchone()[0]
                    collapsed = conn.execute(
                        "SELECT COUNT(*) FROM import_sources WHERE kept = 0"
                    ).fetchone()[0]
                else:
                    for r in conn.execute(
                        "SELECT COALESCE(import_status, 'normal') AS s, COUNT(*) AS n "
                        "FROM conversations GROUP BY COALESCE(import_status, 'normal')"
                    ).fetchall():
                        counts[r["s"]] = r["n"]
                    total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                    synthetic = conn.execute(
                        "SELECT COUNT(*) FROM conversations WHERE id LIKE 'synthetic-%'"
                    ).fetchone()[0]
                    collapsed = 0
            except sqlite3.Error as e:
                self._log_request_error(e, 500)
                self.send_json({"error": _busy_or_failed(e, "The import audit couldn't be loaded.")}, 500); return

            # Content left out, and content of a type this app does not
            # know, added up over every imported file (AUDIT.md #12).
            content_types = {"dropped": {}, "unknown": {}}
            try:
                import build_db
                for row in conn.execute(
                    "SELECT dropped_types, unknown_types FROM imported_backups"
                ).fetchall():
                    found = {}
                    for group, raw in (("dropped", row[0]), ("unknown", row[1])):
                        try:
                            value = json.loads(raw) if raw else {}
                        except ValueError:
                            value = {}
                        found[group] = value if isinstance(value, dict) else {}
                    build_db.add_content_types(content_types, found)
            except sqlite3.Error as e:
                # An older database without the columns still shows the rest.
                errorlog.record_exception(e, action="Read content-type counts")

            self.send_json({
                "dropped_types": content_types["dropped"],
                "unknown_types": content_types["unknown"],
                "total":         total,
                "normal":        counts.get("normal", 0),
                "fallback":      counts.get("fallback", 0),
                "metadata_only": counts.get("metadata_only", 0),
                "parse_error":   counts.get("parse_error", 0),
                "synthetic":     synthetic,
                "collapsed":     collapsed,
                "stored":        conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            })
        finally:
            conn.close()

    # ── Import New Chats ─────────────────────────────────────────────────────────
    # Scans the source folder for backup files not yet imported, identifies each
    # as a Claude or ChatGPT export by its contents, merges it into the database,
    # and renames the source file to Provider-conversations-YYYY-MM-DD.json.

    def _api_imported_backups(self, qs):
        """The import-history table. Provider filtering and sorting happen on the
        client; this returns every recorded backup, newest import first."""
        provider = query_rules.provider_from_qs(qs)
        conn = open_db(self.db_path)
        try:
            if provider:
                rows = conn.execute(
                    "SELECT file_name, provider, first_chat, last_chat, total_chats, "
                    "imported_at, added, updated, unchanged, skipped, errors "
                    "FROM imported_backups WHERE provider = ? "
                    "ORDER BY imported_at DESC, id DESC",
                    (provider,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT file_name, provider, first_chat, last_chat, total_chats, "
                    "imported_at, added, updated, unchanged, skipped, errors "
                    "FROM imported_backups ORDER BY imported_at DESC, id DESC"
                ).fetchall()
            self.send_json({"backups": [dict(r) for r in rows]})
        finally:
            conn.close()

    def _api_import_new(self):
        source_dir = Path(self.source_dir)
        if not source_dir.exists():
            self.send_json({"error": "source folder not found"}, 400); return
        payload = self._read_json_body()
        deleted_mode = str(payload.get("deleted_mode") or "skip").strip().lower()
        if deleted_mode not in ("skip", "all", "review"):
            deleted_mode = "skip"
        review_new_messages = bool(payload.get("review_new_messages"))
        db, source = Path(self.db_path), source_dir
        def work(job):
            import build_db
            job.progress("Backing up before the import…", force=True)
            backups.backup_all(db, "before-import")
            conn = open_db(db)
            try:
                summary = import_new_backups(conn, source, deleted_mode=deleted_mode,
                                             review_new_messages=review_new_messages, job=job)
                conn.commit()
                return summary
            except build_db.NotEnoughSpace as exc:
                conn.rollback()
                raise ApiError(str(exc), 507) from exc
            except background_jobs.Cancelled:
                conn.commit()  # only complete chats; reconcile owns each savepoint
                raise
            except Exception:
                conn.rollback()  # earlier batches remain saved
                raise
            finally:
                conn.close()
                try:
                    _build_source_index(source)
                except Exception as exc:
                    errorlog.record_exception(exc, action="Refresh imported files")
        self.send_json({"job": background_jobs.start(db, "import", work)}, 202)

    # ── Folders ────────────────────────────────────────────────────────────────

    def _api_folders_list(self, qs=None):
        """Return the folders and the conversations they hold (for the sidebar),
        scoped to the toggled side when a provider is given."""
        qs = qs or {}
        provider = query_rules.provider_from_qs(qs)
        prov_folder = f" WHERE provider = '{provider}'" if provider else ""
        prov_item = query_rules.and_provider(provider)
        conn = open_db(self.db_path)
        try:
            folders = conn.execute(
                "SELECT id, name, created_at, updated_at FROM udb.folders"
                + prov_folder + " "
                # Alphabetical by default; a folder that has been used (a chat
                # moved into it → updated_at set) floats to the top by recency.
                "ORDER BY (updated_at IS NULL), updated_at DESC, LOWER(name) ASC"
            ).fetchall()
            rows = conn.execute(
                "SELECT fi.folder_id AS folder_id, fi.pinned AS pinned, "
                f"c.id AS id, {DISPLAY_TITLE} AS title, "
                "c.create_time AS create_time, c.update_time AS update_time, "
                "c.message_count AS message_count, c.preview AS preview "
                "FROM udb.folder_items fi "
                "JOIN conversations c ON c.id = fi.conversation_id "
                f"{META_JOIN} "
                f"WHERE {NOT_DELETED}" + prov_item + " "
                # Pinned chats float to the top within the folder, then newest.
                "ORDER BY fi.pinned DESC, c.update_time DESC, c.create_time DESC"
            ).fetchall()
            # Labels come from one batched query, not a join: a conversation can
            # carry several, and joining would duplicate its folder row.
            folder_labels = labels.labels_by_conv(conn, [r["id"] for r in rows])
            # Condensed summary flags for the sidebar hint.
            folder_cids = [r["id"] for r in rows]
            folder_has_sum = set()
            if folder_cids:
                marks = ",".join("?" * len(folder_cids))
                for sr in conn.execute(
                    "SELECT conversation_id FROM udb.conversation_summaries "
                    f"WHERE conversation_id IN ({marks}) "
                    "AND condensed_summary IS NOT NULL AND TRIM(condensed_summary) != ''",
                    tuple(folder_cids),
                ).fetchall():
                    folder_has_sum.add(sr["conversation_id"])
            by_folder: dict = {}
            for r in rows:
                by_folder.setdefault(r["folder_id"], []).append({
                    "id":            r["id"],
                    "title":         r["title"],
                    "create_time":   r["create_time"],
                    "update_time":   r["update_time"],
                    "message_count": r["message_count"],
                    "pinned":        bool(r["pinned"]),
                    "preview":       r["preview"] or "",
                    "labels":        folder_labels.get(r["id"], []),
                    "has_condensed_summary": r["id"] in folder_has_sum,
                })
            out = [{
                "id":            f["id"],
                "name":          f["name"],
                "created_at":    f["created_at"],
                "updated_at":    f["updated_at"],
                "conversations": by_folder.get(f["id"], []),
            } for f in folders]
            self.send_json({"folders": out})
        finally:
            conn.close()

    def _api_folder_create(self):
        payload = self._read_json_body()
        name = (payload.get("name") or "").strip()
        if not name:
            self.send_json({"error": "missing name"}, 400); return
        # The folder belongs to the side it was created on.
        provider = query_rules.provider_or_none(payload.get("provider")) or "claude"
        fid = uuid.uuid4().hex
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "INSERT INTO udb.folders (id, name, created_at, updated_at, provider) "
                "VALUES (?, ?, ?, NULL, ?)",
                (fid, name, time.time(), provider),
            )
            conn.commit()
            self.send_json({"ok": True, "id": fid, "name": name, "provider": provider})
        finally:
            conn.close()

    def _api_folder_update(self, fid):
        payload = self._read_json_body()
        name = (payload.get("name") or "").strip()
        if not fid or not name:
            self.send_json({"error": "missing name"}, 400); return
        conn = open_db(self.db_path)
        try:
            conn.execute("UPDATE udb.folders SET name = ? WHERE id = ?", (name, fid))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_folder_item_add(self):
        """Move a conversation into a folder. Clears any loose pin and the
        archived flag (foldered chats are not archived, and never show in the
        loose Pinned view)."""
        payload = self._read_json_body()
        fid = (payload.get("folder_id") or "").strip()
        cid = (payload.get("conversation_id") or "").strip()
        if not fid or not cid:
            self.send_json({"error": "missing folder_id or conversation_id"}, 400); return
        now = time.time()
        conn = open_db(self.db_path)
        try:
            folder = conn.execute(
                "SELECT provider FROM udb.folders WHERE id = ?", (fid,)
            ).fetchone()
            if not folder:
                self.send_json({"error": "folder not found"}, 404); return
            # Folders are provider-scoped and folder membership is filtered by
            # provider in both sidebars, so a chat placed in the other provider's
            # folder would vanish from every list. Reject the mismatch outright
            # (a Compare item from the opposite side can reach this path).
            convrow = conn.execute(
                "SELECT provider FROM conversations WHERE id = ?", (cid,)
            ).fetchone()
            if convrow is None:
                self.send_json({"error": "conversation not found"}, 404); return
            folder_provider = folder["provider"]
            conv_provider = convrow["provider"]
            if folder_provider and conv_provider and folder_provider != conv_provider:
                self.send_json(
                    {"error": "folder belongs to a different provider"}, 409
                ); return
            conn.execute("DELETE FROM udb.pinned_conversations WHERE conversation_id = ?", (cid,))
            conn.execute(
                "INSERT OR IGNORE INTO udb.conversation_meta(conversation_id, custom_title, archived, deleted) "
                "VALUES (?, NULL, 0, 0)", (cid,),
            )
            conn.execute("UPDATE udb.conversation_meta SET archived = 0 WHERE conversation_id = ?", (cid,))
            conn.execute(
                "INSERT INTO udb.folder_items (conversation_id, folder_id, added_at, pinned) "
                "VALUES (?, ?, ?, 0) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "  folder_id = excluded.folder_id, added_at = excluded.added_at, pinned = 0",
                (cid, fid, now),
            )
            conn.execute("UPDATE udb.folders SET updated_at = ? WHERE id = ?", (now, fid))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_folder_item_update(self, cid):
        """Toggle the pinned-within-folder flag for one conversation."""
        payload = self._read_json_body()
        if not cid:
            self.send_json({"error": "missing conversation id"}, 400); return
        conn = open_db(self.db_path)
        try:
            if "pinned" in payload:
                conn.execute(
                    "UPDATE udb.folder_items SET pinned = ? WHERE conversation_id = ?",
                    (1 if payload.get("pinned") else 0, cid),
                )
                conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_folder_item_remove(self, cid):
        """Remove a conversation from its folder → it returns to the main list."""
        if not cid:
            self.send_json({"error": "missing conversation id"}, 400); return
        conn = open_db(self.db_path)
        try:
            conn.execute("DELETE FROM udb.folder_items WHERE conversation_id = ?", (cid,))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_memories(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute("SELECT id, memory_id, content FROM memories ORDER BY id").fetchall()
            self.send_json({"memories": [dict(r) for r in rows]})
        except Exception:
            self.send_json({"memories": []})
        finally:
            conn.close()

    def _api_projects(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT p.id, p.name, p.description, p.create_time, p.update_time, "
                "COUNT(d.id) AS doc_count "
                "FROM projects p LEFT JOIN project_docs d ON d.project_id = p.id "
                "GROUP BY p.id "
                "ORDER BY p.update_time DESC"
            ).fetchall()
            self.send_json({"projects": [dict(r) for r in rows]})
        except Exception:
            self.send_json({"projects": []})
        finally:
            conn.close()

    def _api_project(self, project_id):
        conn = open_db(self.db_path)
        try:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj:
                self.send_error(404); return
            docs = conn.execute(
                "SELECT id, filename, content, create_time FROM project_docs "
                "WHERE project_id = ? ORDER BY create_time", (project_id,),
            ).fetchall()
            self.send_json({"project": dict(proj), "docs": [dict(d) for d in docs]})
        finally:
            conn.close()

    def _api_artifact(self, artifact_id, qs):
        # An artifact is (chat, artifact name): the same name can be in
        # several chats (AUDIT.md #5), so the chat is required.
        conv_id = (qs.get("conv") or [""])[0]
        if not conv_id:
            raise ApiError("Say which chat the artifact is in.", 400)
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT id, conv_id, msg_seq, title, type, lang, content "
                "FROM artifacts WHERE conv_id = ? AND id = ?", (conv_id, artifact_id),
            ).fetchone()
            if row:
                self.send_json(dict(row))
            else:
                raise ApiError("This artifact isn't in this chat.", 404)
        finally:
            conn.close()

    # ── Claude model availability ────────────────────────────────────────────

    def _dataset_format(self, conn) -> str:
        """'claude' or 'chatgpt' for the loaded export.

        build_db records this at index time. Databases built before the model
        feature existed have no record, so sniff the head of the export once
        and store the answer rather than re-reading a large file every request.
        """
        try:
            row = conn.execute(
                "SELECT pref_value FROM udb.ui_preferences WHERE pref_key = 'dataset_format'"
            ).fetchone()
        except sqlite3.Error:
            return "claude"
        if row:
            try:
                value = json.loads(row["pref_value"])
            except Exception:
                value = row["pref_value"]
            if value in query_rules.PROVIDERS:
                return value
        fmt = "claude"
        try:
            with open(Path(self.source_dir) / "conversations.json", encoding="utf-8",
                      errors="replace") as f:
                head = f.read(8192)
            fmt = "claude" if '"chat_messages"' in head else "chatgpt"
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT OR REPLACE INTO udb.ui_preferences(pref_key, pref_value) VALUES "
                "('dataset_format', ?)", (json.dumps(fmt),),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        return fmt

    def _model_rows(self, conn):
        """Every model period, newest model first, a model's own periods newest
        first inside its group — so a model that returned after a gap keeps its
        rows together."""
        try:
            rows = conn.execute(
                "SELECT p.id AS period_id, p.model_id, m.name, p.start_date, p.end_date "
                "FROM udb.claude_model_periods p "
                "JOIN udb.claude_models m ON m.id = p.model_id"
            ).fetchall()
        except sqlite3.Error:
            return []
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["model_id"], []).append(dict(r))
        ordered = sorted(
            groups.values(),
            key=lambda g: min(p["start_date"] for p in g),
            reverse=True,
        )
        out = []
        for group in ordered:
            out.extend(sorted(group, key=lambda p: p["start_date"], reverse=True))
        return out

    def _api_claude_models(self):
        conn = open_db(self.db_path)
        try:
            self.send_json({"models": self._model_rows(conn),
                            "format": self._dataset_format(conn)})
        finally:
            conn.close()

    def _reload_claude_models_from_seed(self, conn) -> dict:
        """Re-import claude_models.json's periods into the user-data DB.

        Unlike the one-time startup seed (_seed_claude_models), this runs on
        demand — the "Reload Model Data" button — to pick up corrections to
        the shipped file, such as periods that were missing an end date on
        an earlier import. Models are matched by the file's id, never by
        name, so a model the user renamed keeps its name and is not copied
        (AUDIT.md #11). A matched model's periods are replaced with exactly
        what the file lists; a model the user added by hand, with no entry
        in the file, is left untouched.
        """
        try:
            with open(MODEL_SEED_FILE, encoding="utf-8") as f:
                rows = json.load(f).get("models") or []
        except Exception as e:
            self._log_request_error(e)
            return {"error": "The app's model list file couldn't be read."}

        now = time.time()
        touched: set[str] = set()
        added = 0
        for entry in rows:
            name = str(entry.get("name") or "").strip()
            start = str(entry.get("start_date") or "").strip()
            if not name or not start:
                continue
            end = entry.get("end_date")
            end = str(end).strip() if end else None
            model_id = str(entry.get("id") or "").strip()
            if not model_id:
                continue
            row = conn.execute(
                "SELECT id FROM udb.claude_models WHERE id = ?", (model_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO udb.claude_models(id, name, created_at) VALUES (?, ?, ?)",
                    (model_id, name, now),
                )
                added += 1
            if model_id not in touched:
                # First entry seen for this model in this reload: clear its
                # old periods so a stale/broken one (e.g. a prior import
                # that lost its end date) doesn't linger next to the fix.
                conn.execute(
                    "DELETE FROM udb.claude_model_periods WHERE model_id = ?", (model_id,),
                )
                touched.add(model_id)
            conn.execute(
                "INSERT INTO udb.claude_model_periods(id, model_id, start_date, end_date, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, model_id, start, end, now),
            )
        conn.commit()
        return {"ok": True, "models_touched": len(touched), "models_added": added}

    def _api_claude_models_reload(self):
        conn = open_db(self.db_path)
        try:
            result = self._reload_claude_models_from_seed(conn)
            if result.get("error"):
                self.send_json(result, 400); return
            self.send_json({**result, "models": self._model_rows(conn),
                            "format": self._dataset_format(conn)})
        finally:
            conn.close()

    def _api_claude_model_add(self):
        payload = self._read_json_body()
        name  = str(payload.get("name") or "").strip()
        start = _norm_date(payload.get("start_date"))
        # An omitted end date means "still available"; a malformed one is an
        # error. Collapsing the two would turn a typo into an open-ended period.
        raw_end = str(payload.get("end_date") or "").strip()
        end = None if not raw_end else _norm_date(raw_end)
        if not name:
            self.send_json({"error": "Model Name is required"}, 400); return
        if not start:
            self.send_json({"error": "Beginning date is required (YYYY-MM-DD)"}, 400); return
        if raw_end and not end:
            self.send_json({"error": "End date must be YYYY-MM-DD"}, 400); return
        if end and end <= start:
            self.send_json({"error": "End date must be after the beginning date"}, 400); return
        conn = open_db(self.db_path)
        try:
            # Re-using an existing name adds a second availability window to
            # that same model rather than creating a duplicate model.
            row = conn.execute(
                "SELECT id FROM udb.claude_models WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            now = time.time()
            model_id = row["id"] if row else uuid.uuid4().hex
            if not row:
                conn.execute(
                    "INSERT INTO udb.claude_models(id, name, created_at) VALUES (?, ?, ?)",
                    (model_id, name, now),
                )
            conn.execute(
                "INSERT INTO udb.claude_model_periods(id, model_id, start_date, end_date, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, model_id, start, end, now),
            )
            conn.commit()
            self.send_json({"ok": True, "models": self._model_rows(conn)})
        finally:
            conn.close()

    def _api_claude_model_update(self, period_id):
        payload = self._read_json_body()
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT model_id, start_date, end_date FROM udb.claude_model_periods WHERE id = ?",
                (period_id,),
            ).fetchone()
            if not row:
                self.send_json({"error": "Unknown model row"}, 404); return
            start, end = row["start_date"], row["end_date"]
            if "start_date" in payload:
                start = _norm_date(payload.get("start_date"))
                if not start:
                    self.send_json({"error": "Beginning date is required (YYYY-MM-DD)"}, 400); return
            if "end_date" in payload:
                raw = str(payload.get("end_date") or "").strip()
                end = None if not raw else _norm_date(raw)
                if raw and not end:
                    self.send_json({"error": "End date must be YYYY-MM-DD"}, 400); return
            if end and end <= start:
                self.send_json({"error": "End date must be after the beginning date"}, 400); return
            if "name" in payload:
                name = str(payload.get("name") or "").strip()
                if not name:
                    self.send_json({"error": "Model Name is required"}, 400); return
                # The name belongs to the model, so editing it in any row
                # renames the model and every availability window it has.
                conn.execute("UPDATE udb.claude_models SET name = ? WHERE id = ?",
                             (name, row["model_id"]))
            conn.execute(
                "UPDATE udb.claude_model_periods SET start_date = ?, end_date = ? WHERE id = ?",
                (start, end, period_id),
            )
            conn.commit()
            self.send_json({"ok": True, "models": self._model_rows(conn)})
        finally:
            conn.close()

    def _api_claude_model_remove(self, period_id):
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT model_id FROM udb.claude_model_periods WHERE id = ?", (period_id,)
            ).fetchone()
            if not row:
                self.send_json({"error": "Unknown model row"}, 404); return
            conn.execute("DELETE FROM udb.claude_model_periods WHERE id = ?", (period_id,))
            # Dropping a model's last window drops the model, and with it any
            # conversation that pointed at it.
            left = conn.execute(
                "SELECT COUNT(*) FROM udb.claude_model_periods WHERE model_id = ?",
                (row["model_id"],),
            ).fetchone()[0]
            if not left:
                conn.execute("DELETE FROM udb.claude_models WHERE id = ?", (row["model_id"],))
                conn.execute("DELETE FROM udb.conversation_models WHERE model_id = ?",
                             (row["model_id"],))
            conn.commit()
            self.send_json({"ok": True, "models": self._model_rows(conn)})
        finally:
            conn.close()

    def _conv_model_state(self, conn, conv_id):
        """Selected models, the models still offerable for this conversation,
        and the two warning states — everything the header strip needs."""
        conv = conn.execute(
            "SELECT create_time, update_time FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if not conv:
            return None
        first_day, last_day = _conv_date_range(conv["create_time"], conv["update_time"])
        rows = self._model_rows(conn)
        by_model: dict[str, dict] = {}
        for r in rows:
            entry = by_model.setdefault(
                r["model_id"], {"model_id": r["model_id"], "name": r["name"], "periods": []}
            )
            entry["periods"].append((r["start_date"], r["end_date"]))
        chosen = [
            r["model_id"] for r in conn.execute(
                "SELECT model_id FROM udb.conversation_models WHERE conversation_id = ? "
                "ORDER BY added_at", (conv_id,)
            ).fetchall()
        ]
        chosen = [mid for mid in chosen if mid in by_model]
        selected = [{"model_id": mid, "name": by_model[mid]["name"]} for mid in chosen]
        # Offer only models whose availability overlaps the conversation, and
        # never one that is already selected.
        available = []
        if first_day:
            for entry in by_model.values():
                if entry["model_id"] in chosen:
                    continue
                if any(_period_overlaps(s, e, first_day, last_day) for s, e in entry["periods"]):
                    available.append({"model_id": entry["model_id"], "name": entry["name"]})
        flags = _compute_conv_flags(
            conn, [(conv_id, conv["create_time"], conv["update_time"])]
        ).get(conv_id, {"warn_range": False, "warn_coverage": False})
        return {
            "selected": selected,
            "available": available,
            "warn_range": flags["warn_range"],
            "warn_coverage": flags["warn_coverage"],
        }

    def _api_conversation_models(self, qs):
        conv_id = ((qs.get("conv_id") or [""])[0]).strip()
        conn = open_db(self.db_path)
        try:
            state = self._conv_model_state(conn, conv_id)
            if state is None:
                self.send_json({"error": "Unknown conversation"}, 404); return
            self.send_json(state)
        finally:
            conn.close()

    def _api_conversation_model_add(self):
        payload = self._read_json_body()
        conv_id  = str(payload.get("conv_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip()
        if not conv_id or not model_id:
            self.send_json({"error": "conv_id and model_id are required"}, 400); return
        conn = open_db(self.db_path)
        try:
            if not conn.execute("SELECT 1 FROM udb.claude_models WHERE id = ?",
                                (model_id,)).fetchone():
                self.send_json({"error": "Unknown model"}, 404); return
            conn.execute(
                "INSERT OR IGNORE INTO udb.conversation_models(conversation_id, model_id, added_at) "
                "VALUES (?, ?, ?)", (conv_id, model_id, time.time()),
            )
            conn.commit()
            state = self._conv_model_state(conn, conv_id)
            if state is None:
                self.send_json({"error": "Unknown conversation"}, 404); return
            self.send_json(state)
        finally:
            conn.close()

    def _api_conversation_model_remove(self, conv_id, model_id):
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "DELETE FROM udb.conversation_models WHERE conversation_id = ? AND model_id = ?",
                (conv_id, model_id),
            )
            conn.commit()
            state = self._conv_model_state(conn, conv_id)
            if state is None:
                self.send_json({"error": "Unknown conversation"}, 404); return
            self.send_json(state)
        finally:
            conn.close()

    def _api_conversation_model_dismiss(self):
        payload = self._read_json_body()
        conv_id = str(payload.get("conv_id") or "").strip()
        kind    = str(payload.get("kind") or "").strip()
        if not conv_id or kind not in ("range", "coverage"):
            self.send_json({"error": "conv_id and kind ('range'|'coverage') are required"}, 400)
            return
        column = "range_dismissed" if kind == "range" else "coverage_dismissed"
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO udb.conversation_model_flags(conversation_id) VALUES (?)",
                (conv_id,),
            )
            conn.execute(
                f"UPDATE udb.conversation_model_flags SET {column} = 1 WHERE conversation_id = ?",
                (conv_id,),
            )
            conn.commit()
            state = self._conv_model_state(conn, conv_id)
            if state is None:
                self.send_json({"error": "Unknown conversation"}, 404); return
            self.send_json(state)
        finally:
            conn.close()

    # ── Conversation tags ────────────────────────────────────────────────────
    # Free-form tags, stored in the persistent user-data DB (see
    # conversation_tags in _ensure_userdata_schema) so they survive a
    # history.db rebuild the way folders and model choices do.

    def _conv_tags(self, conn, conv_id):
        rows = conn.execute(
            "SELECT tag FROM udb.conversation_tags WHERE conversation_id = ? "
            "ORDER BY added_at", (conv_id,),
        ).fetchall()
        return [r["tag"] for r in rows]

    def _api_tags_all(self):
        """Every distinct tag in use, for the add-tag autocomplete."""
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT tag FROM udb.conversation_tags ORDER BY tag COLLATE NOCASE"
            ).fetchall()
            self.send_json({"tags": [r["tag"] for r in rows]})
        finally:
            conn.close()

    def _api_tag_add(self):
        payload = self._read_json_body()
        conv_id = str(payload.get("conv_id") or "").strip()
        # Collapse internal whitespace along with trimming the ends, so
        # "  UI   Test " and "UI Test" land as the same tag.
        tag = check_text("Tag", " ".join(str(payload.get("tag") or "").split()), FIELD_LIMITS["tag"])
        if not conv_id or not tag:
            self.send_json({"error": "conv_id and tag are required"}, 400); return
        conn = open_db(self.db_path)
        try:
            if not conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone():
                self.send_json({"error": "Unknown conversation"}, 404); return
            conn.execute(
                "INSERT OR IGNORE INTO udb.conversation_tags(conversation_id, tag, added_at, tag_id) "
                "VALUES (?, ?, ?, ?)", (conv_id, tag, time.time(), tag_ids.tag_id(conn, "tag", tag)),
            )
            conn.commit()
            self.send_json({"tags": self._conv_tags(conn, conv_id)})
        finally:
            conn.close()

    def _api_tag_remove(self, conv_id, tag):
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "DELETE FROM udb.conversation_tags WHERE conversation_id = ? AND tag = ?",
                (conv_id, tag),
            )
            conn.commit()
            self.send_json({"tags": self._conv_tags(conn, conv_id)})
        finally:
            conn.close()

    # ── Mood tags ─────────────────────────────────────────────────────────────
    # A second, independent per-conversation tag set with the same normalization
    # and persistence rules as ordinary tags, but its own table and vocabulary.

    def _conv_mood_tags(self, conn, conv_id):
        rows = conn.execute(
            "SELECT tag FROM udb.conversation_mood_tags WHERE conversation_id = ? "
            "ORDER BY added_at", (conv_id,),
        ).fetchall()
        return [r["tag"] for r in rows]

    def _api_mood_tags_all(self):
        """Every distinct mood tag in use, for Mood Tag autocomplete."""
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT tag FROM udb.conversation_mood_tags "
                "ORDER BY tag COLLATE NOCASE"
            ).fetchall()
            self.send_json({"tags": [r["tag"] for r in rows]})
        finally:
            conn.close()

    def _api_mood_tag_add(self):
        payload = self._read_json_body()
        conv_id = str(payload.get("conv_id") or "").strip()
        tag = check_text("Tag", " ".join(str(payload.get("tag") or "").split()), FIELD_LIMITS["tag"])
        if not conv_id or not tag:
            self.send_json({"error": "conv_id and tag are required"}, 400); return
        conn = open_db(self.db_path)
        try:
            if not conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone():
                self.send_json({"error": "Unknown conversation"}, 404); return
            conn.execute(
                "INSERT OR IGNORE INTO udb.conversation_mood_tags"
                "(conversation_id, tag, added_at, tag_id) VALUES (?, ?, ?, ?)",
                (conv_id, tag, time.time(), tag_ids.tag_id(conn, "mood", tag)),
            )
            conn.commit()
            self.send_json({"tags": self._conv_mood_tags(conn, conv_id)})
        finally:
            conn.close()

    def _api_mood_tag_remove(self, conv_id, tag):
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "DELETE FROM udb.conversation_mood_tags "
                "WHERE conversation_id = ? AND tag = ?",
                (conv_id, tag),
            )
            conn.commit()
            self.send_json({"tags": self._conv_mood_tags(conn, conv_id)})
        finally:
            conn.close()

    # ── Conversation labels · bulk labeling · snapshots ──────────────────────
    # The reusable business/data logic for these features lives in the labels,
    # bulk_labels, and snapshots modules. The handlers below stay thin: read the
    # request data, call the module function with an open connection, and send
    # its result (ApiError → JSON error with its status, via _handle_db).

    # ── Conversation labels ──────────────────────────────────────────────────
    def _api_labels_list(self):
        self._handle_db(labels.list_labels)

    def _api_label_create(self):
        payload = self._read_json_body()
        self._handle_db(lambda conn: labels.create_label(conn, payload))

    def _api_label_update(self, lid):
        payload = self._read_json_body()
        self._handle_db(lambda conn: labels.update_label(conn, lid, payload))

    def _api_label_delete(self, lid):
        self._handle_db(lambda conn: labels.delete_label(conn, lid))

    def _api_labels_reorder(self):
        payload = self._read_json_body()
        self._handle_db(lambda conn: labels.reorder_labels(conn, payload))

    def _api_conversation_label_get(self, conv_id):
        self._handle_db(lambda conn: labels.get_conv_label(conn, conv_id))

    def _api_conversation_label_set(self):
        payload = self._read_json_body()
        self._handle_db(lambda conn: labels.set_conv_label(conn, payload))

    # ── Conditional bulk labeling ────────────────────────────────────────────
    def _api_bulk_preview(self):
        crit = self._read_json_body()
        self._handle_db(lambda conn: bulk_labels.bulk_preview(conn, crit))

    def _api_bulk_preview_list(self):
        """The one bulk handler that is not a straight _handle_db wrapper: the
        rows go out through the shared conversation-list renderer, so the
        module returns the page and this sends it."""
        payload = self._read_json_body()
        conn = open_db(self.db_path)
        try:
            rows, total, offset, limit, provider = \
                bulk_labels.bulk_preview_page(conn, payload)
            self._send_conv_list(conn, rows, total, offset, limit, provider)
        except ApiError as e:
            self.send_json({"error": e.message}, e.status)
        finally:
            conn.close()

    def _api_bulk_apply(self):
        crit = self._read_json_body()
        self._handle_db(lambda conn: bulk_labels.bulk_apply(conn, crit))

    def _api_bulk_run_list(self):
        self._handle_db(bulk_labels.bulk_run_list)

    def _api_bulk_run_continue(self):
        payload = self._read_json_body()
        self._handle_db(lambda conn: bulk_labels.bulk_run_continue(conn, payload))

    def _api_bulk_run_delete(self, run_id):
        self._handle_db(lambda conn: bulk_labels.bulk_run_delete(conn, run_id))

    def _api_bulk_history(self):
        self._handle_db(bulk_labels.bulk_history)

    def _api_bulk_retention_set(self):
        payload = self._read_json_body()
        self._handle_db(lambda conn: bulk_labels.bulk_retention_set(conn, payload))

    # ── Manual safety snapshots ──────────────────────────────────────────────
    def _api_snapshot_list(self):
        self._handle_db(snapshots.snapshot_list)

    def _api_snapshot_create(self):
        payload = self._read_json_body()
        self._handle_db(lambda conn: snapshots.snapshot_create(conn, payload))

    def _api_snapshot_delete(self, sid):
        self._handle_db(lambda conn: snapshots.snapshot_delete(conn, sid))

    def _api_snapshot_restore(self, sid):
        body = self._read_json_body()
        self._handle_db(lambda conn: snapshots.snapshot_restore(conn, sid, body))
    def _api_preferences(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute("SELECT pref_key, pref_value FROM udb.ui_preferences").fetchall()
            prefs = {}
            for r in rows:
                if r["pref_key"] in INTERNAL_PREF_KEYS:
                    continue
                try:
                    prefs[r["pref_key"]] = json.loads(r["pref_value"])
                except Exception:
                    prefs[r["pref_key"]] = r["pref_value"]
            self.send_json({"preferences": prefs})
        finally:
            conn.close()

    def _api_preferences_update(self):
        payload = self._read_json_body()
        prefs = payload.get("preferences") if isinstance(payload.get("preferences"), dict) else payload
        if isinstance(prefs, dict):
            request_limits.check_preferences(prefs)
        if not isinstance(prefs, dict):
            self.send_json({"error": "Invalid preferences payload"}, 400); return
        conn = open_db(self.db_path)
        try:
            for k, v in prefs.items():
                # The page sends back every setting it loaded. A key the app
                # keeps for itself is left alone, never overwritten (AUDIT.md #19).
                if str(k) in INTERNAL_PREF_KEYS:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO udb.ui_preferences(pref_key, pref_value) VALUES (?, ?)",
                    (str(k), json.dumps(v, ensure_ascii=False)),
                )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    # ── Conversation summaries ──────────────────────────────────────────────

    def _api_summary_get(self, conv_id):
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT condensed_summary, summary, condensed_author, "
                "summary_author, updated_at, version "
                "FROM udb.conversation_summaries WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            if row:
                self.send_json({"conversation_id": conv_id,
                                "condensed_summary": row["condensed_summary"] or "",
                                "summary": row["summary"] or "",
                                "condensed_author": row["condensed_author"] or "",
                                "summary_author": row["summary_author"] or "",
                                "updated_at": row["updated_at"],
                                "version": row["version"] or 0})
            else:
                self.send_json({"conversation_id": conv_id,
                                "condensed_summary": "", "summary": "",
                                "condensed_author": "", "summary_author": "",
                                "updated_at": None, "version": 0})
        finally:
            conn.close()

    def _api_summary_update(self, conv_id):
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        payload = self._read_json_body()
        base = _base_version(payload)
        conn = open_db(self.db_path)
        try:
            import time as _time
            now = _time.time()
            # Check the version and write under one lock, so two saves can't
            # both pass the check.
            conn.execute("BEGIN IMMEDIATE")
            _check_version(conn, "udb.conversation_summaries", conv_id, base,
                           "This summary was changed in another window since you opened it.")
            author = payload.get("author", "personal")
            if author not in ("personal", "ai"):
                author = "personal"

            cond_author = None
            summ_author = None
            if payload.get("condensed_summary") is not None:
                cond_author = author
            if payload.get("summary") is not None:
                summ_author = author

            existing = conn.execute(
                "SELECT condensed_author, summary_author "
                "FROM udb.conversation_summaries WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            if existing:
                if cond_author and existing["condensed_author"] \
                        and existing["condensed_author"] != cond_author \
                        and existing["condensed_author"] != "collaborative":
                    cond_author = "collaborative"
                if summ_author and existing["summary_author"] \
                        and existing["summary_author"] != summ_author \
                        and existing["summary_author"] != "collaborative":
                    summ_author = "collaborative"

            conn.execute(
                "INSERT INTO udb.conversation_summaries "
                "(conversation_id, condensed_summary, summary, "
                " condensed_author, summary_author, updated_at, version) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "condensed_summary = COALESCE(excluded.condensed_summary, condensed_summary), "
                "summary = COALESCE(excluded.summary, summary), "
                "condensed_author = COALESCE(excluded.condensed_author, condensed_author), "
                "summary_author = COALESCE(excluded.summary_author, summary_author), "
                "updated_at = excluded.updated_at, version = COALESCE(version, 0) + 1",
                (conv_id,
                 payload.get("condensed_summary"),
                 payload.get("summary"),
                 cond_author,
                 summ_author,
                 now),
            )
            row = conn.execute(
                "SELECT condensed_author, summary_author, version "
                "FROM udb.conversation_summaries WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            conn.commit()
            self.send_json({"ok": True, "updated_at": now,
                            "condensed_author": (row["condensed_author"] or "") if row else "",
                            "summary_author": (row["summary_author"] or "") if row else "",
                            "version": (row["version"] or 0) if row else 0})
        finally:
            conn.close()

    # ── Conversation notes ─────────────────────────────────────────────────
    # These are independent fields by design. A later AI/API feature can choose
    # whether to read them for a run and can write notes_from_ai without
    # coupling that write to what was supplied as input.

    _NOTE_FIELDS = ("user_notes", "notes_to_ai", "notes_from_ai")

    def _api_notes_get(self, conv_id):
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT user_notes, notes_to_ai, notes_from_ai, updated_at, version "
                "FROM udb.conversation_notes WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            values = {
                field: (row[field] or "") if row else ""
                for field in self._NOTE_FIELDS
            }
            self.send_json({
                "conversation_id": conv_id,
                **values,
                "updated_at": row["updated_at"] if row else None,
                "version": (row["version"] or 0) if row else 0,
            })
        finally:
            conn.close()

    def _api_notes_update(self, conv_id):
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        payload = self._read_json_body()
        updates = {}
        for field in self._NOTE_FIELDS:
            if field not in payload:
                continue
            if not isinstance(payload[field], str):
                self.send_json({"error": f"{request_limits.field_label(field)} must be text."}, 400); return
            updates[field] = payload[field]
        if not updates:
            self.send_json({"error": "no note fields supplied"}, 400); return
        base = _base_version(payload)

        conn = open_db(self.db_path)
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            _check_version(conn, "udb.conversation_notes", conv_id, base,
                           "These notes were changed in another window since you opened them.")
            conn.execute(
                "INSERT INTO udb.conversation_notes "
                "(conversation_id, user_notes, notes_to_ai, notes_from_ai, updated_at, version) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "user_notes = COALESCE(excluded.user_notes, user_notes), "
                "notes_to_ai = COALESCE(excluded.notes_to_ai, notes_to_ai), "
                "notes_from_ai = COALESCE(excluded.notes_from_ai, notes_from_ai), "
                "updated_at = excluded.updated_at, version = COALESCE(version, 0) + 1",
                (
                    conv_id,
                    updates.get("user_notes"),
                    updates.get("notes_to_ai"),
                    updates.get("notes_from_ai"),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT user_notes, notes_to_ai, notes_from_ai, version "
                "FROM udb.conversation_notes WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            conn.commit()
            self.send_json({
                "ok": True,
                "conversation_id": conv_id,
                **{field: row[field] or "" for field in self._NOTE_FIELDS},
                "updated_at": now,
                "version": row["version"] or 0,
            })
        finally:
            conn.close()

    # ── Bookmarks ────────────────────────────────────────────────────────────
    # A bookmark marks one message in a conversation. It has its own stable id
    # (UUID) so a rename never changes what identifies it; the conversation id
    # is never touched here.
    def _conv_bookmarks(self, conn, conv_id):
        # Only bookmarks whose message is still in the chat are placed on it; one
        # whose message is gone never moves onto a different message.
        message_ids.reanchor_bookmarks(conn, conv_id)
        rows = conn.execute(
            "SELECT id, conversation_id, seq, name, source, created_at, updated_at, message_uuid "
            "FROM udb.conversation_bookmarks WHERE conversation_id = ? ORDER BY seq",
            (conv_id,),
        ).fetchall()
        return [dict(r) for r in rows if not message_ids.bookmark_missing(conn, conv_id, r)]

    def _api_bookmarks_get(self, conv_id):
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        conn = open_db(self.db_path)
        try:
            self.send_json({"conversation_id": conv_id,
                            "bookmarks": self._conv_bookmarks(conn, conv_id)})
        finally:
            conn.close()

    def _api_global_bookmarks(self, qs):
        # Every conversation that carries at least one bookmark (see
        # _global_marked_conversations), each with its bookmarks.
        def bookmarks_of(conn, cid):
            message_ids.reanchor_bookmarks(conn, cid)
            bms = conn.execute(
                "SELECT id, seq, name, message_uuid FROM udb.conversation_bookmarks "
                "WHERE conversation_id = ? ORDER BY seq",
                (cid,),
            ).fetchall()
            # A bookmark whose message is gone stays listed (it is the
            # user's), marked missing so it never opens another message.
            return {"bookmarks": [
                {**dict(b), "missing": message_ids.bookmark_missing(conn, cid, b)}
                for b in bms
            ]}
        self._global_marked_conversations(
            qs, table="conversation_bookmarks", scope_name="bookmarks",
            search_sql="b2.name LIKE ?", search_params=1, items=bookmarks_of)

    def _api_global_sticky_notes(self, qs):
        # Every conversation with at least one message sticky note, each with
        # its notes (title search covers the note's title and text).
        def notes_of(conn, cid):
            notes = []
            for n in conn.execute(
                "SELECT id, seq, title, message_uuid FROM udb.sticky_notes "
                "WHERE conversation_id = ? ORDER BY seq",
                (cid,),
            ).fetchall():
                seq = message_ids.locate(conn, cid, n["message_uuid"], n["seq"])
                notes.append({**dict(n), "seq": n["seq"] if seq is None else seq,
                              "missing": seq is None})
            notes.sort(key=lambda n: n["seq"])
            return {"sticky_notes": notes}
        self._global_marked_conversations(
            qs, table="sticky_notes", scope_name="sticky_notes",
            search_sql="(b2.title LIKE ? OR b2.body LIKE ?)", search_params=2, items=notes_of)

    def _global_marked_conversations(self, qs, *, table, scope_name, search_sql, search_params, items):
        # Conversations with at least one row in `table` (bookmarks or sticky
        # notes), scoped to the side (Claude / ChatGPT) currently in view — the
        # two never mix, like the rest of the app. Ordered by the last message
        # the *user* sent in each conversation, newest first by default.
        # Paginated 25 conversations at a time (the rows are not counted toward
        # the page size). An optional search narrows by conversation title, the
        # rows' own text, or both.
        provider = query_rules.provider_from_qs(qs)

        sort = ((qs.get("sort") or ["newest"])[0]).strip().lower()
        order = "ASC" if sort == "oldest" else "DESC"

        scope = ((qs.get("scope") or ["both"])[0]).strip().lower()
        if scope not in ("titles", scope_name, "both"):
            scope = "both"
        q = ((qs.get("q") or [""])[0]).strip()

        try:
            limit = int((qs.get("limit") or ["25"])[0])
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 200))
        try:
            offset = int((qs.get("offset") or ["0"])[0])
        except (TypeError, ValueError):
            offset = 0

        conn = open_db(self.db_path)
        try:
            where = [
                f"EXISTS (SELECT 1 FROM udb.{table} b "
                "WHERE b.conversation_id = c.id)",
                NOT_DELETED,
            ]
            params = []
            if provider:
                where.append(query_rules.is_provider(provider))
            if q:
                like = f"%{q}%"
                terms = []
                if scope in ("titles", "both"):
                    terms.append(
                        f"{DISPLAY_TITLE} LIKE ?")
                    params.append(like)
                if scope in (scope_name, "both"):
                    terms.append(
                        f"EXISTS (SELECT 1 FROM udb.{table} b2 "
                        f"WHERE b2.conversation_id = c.id AND {search_sql})")
                    params.extend([like] * search_params)
                where.append("(" + " OR ".join(terms) + ")")
            where_sql = " AND ".join(where)

            # Last user message time. NULL (no user message) sorts to the end in
            # either direction so the dated conversations stay grouped together.
            last_expr = (
                "(SELECT MAX(m.create_time) FROM messages m "
                "WHERE m.conversation_id = c.id AND m.role = 'user')")

            total = conn.execute(
                "SELECT COUNT(*) FROM conversations c "
                f"{META_JOIN} "
                "WHERE " + where_sql,
                tuple(params),
            ).fetchone()[0]

            rows = conn.execute(
                f"SELECT c.id, {DISPLAY_TITLE} AS title, "
                "c.provider, " + last_expr + " AS last_user_time "
                "FROM conversations c "
                f"{META_JOIN} "
                "WHERE " + where_sql + " "
                "ORDER BY last_user_time IS NULL, "
                f"last_user_time {order}, c.update_time {order} "
                "LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()

            convs = [{
                "id": r["id"],
                "title": r["title"],
                "provider": r["provider"],
                "last_user_time": r["last_user_time"],
                **items(conn, r["id"]),
            } for r in rows]

            self.send_json({
                "conversations": convs,
                "total": total,
                "offset": offset,
                "limit": limit,
                "sort": sort,
                "scope": scope,
                "q": q,
            })
        finally:
            conn.close()

    def _api_bookmark_create(self):
        payload = self._read_json_body()
        conv_id = (payload.get("conversation_id") or "").strip()
        seq = payload.get("seq")
        if not conv_id or seq is None:
            self.send_json({"error": "missing conversation id or seq"}, 400); return
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            self.send_json({"error": "invalid seq"}, 400); return
        name = payload.get("name")
        if name is not None:
            name = str(name)
        source = payload.get("source") or "user"
        conn = open_db(self.db_path)
        try:
            now = time.time()
            # Toggling the same message on twice must not make a second bookmark:
            # return the one already there. A bookmark belongs to a message by
            # its id; position only identifies one made without an id.
            message_uuid = message_ids.message_uuid_at(conn, conv_id, seq)
            message_ids.reanchor_bookmarks(conn, conv_id)
            if message_uuid:
                existing = conn.execute(
                    "SELECT id FROM udb.conversation_bookmarks "
                    "WHERE conversation_id = ? AND message_uuid = ?",
                    (conv_id, message_uuid),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id FROM udb.conversation_bookmarks "
                    "WHERE conversation_id = ? AND seq = ? AND message_uuid IS NULL",
                    (conv_id, seq),
                ).fetchone()
            if existing:
                bid = existing["id"]
            else:
                bid = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO udb.conversation_bookmarks "
                        "(id, conversation_id, seq, name, source, created_at, updated_at, message_uuid, message_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (bid, conv_id, seq, name, source, now, now, message_uuid,
                         message_ids.bookmark_fingerprint_at(conn, conv_id, seq)),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    # Another request bookmarked the same message first: return it.
                    conn.rollback()
                    bid = conn.execute(
                        "SELECT id FROM udb.conversation_bookmarks WHERE conversation_id = ? "
                        "AND (message_uuid = ? OR (message_uuid IS NULL AND seq = ?))",
                        (conv_id, message_uuid, seq),
                    ).fetchone()["id"]
            row = conn.execute(
                "SELECT id, conversation_id, seq, name, source, created_at, updated_at, message_uuid "
                "FROM udb.conversation_bookmarks WHERE id = ?",
                (bid,),
            ).fetchone()
            self.send_json({"ok": True, "bookmark": dict(row)})
        finally:
            conn.close()

    def _api_bookmark_update(self, bid):
        if not bid:
            self.send_json({"error": "missing bookmark id"}, 400); return
        payload = self._read_json_body()
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM udb.conversation_bookmarks WHERE id = ?", (bid,),
            ).fetchone()
            if not row:
                self.send_json({"error": "not found"}, 404); return
            # Only the name is editable. The id stays put so references hold.
            if payload.get("name") is not None:
                conn.execute(
                    "UPDATE udb.conversation_bookmarks SET name = ?, updated_at = ? "
                    "WHERE id = ?",
                    (str(payload.get("name")), time.time(), bid),
                )
                conn.commit()
            out = conn.execute(
                "SELECT id, conversation_id, seq, name, source, created_at, updated_at, message_uuid "
                "FROM udb.conversation_bookmarks WHERE id = ?", (bid,),
            ).fetchone()
            self.send_json({"ok": True, "bookmark": dict(out)})
        finally:
            conn.close()

    def _api_bookmark_delete(self, bid):
        if not bid:
            self.send_json({"error": "missing bookmark id"}, 400); return
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "DELETE FROM udb.conversation_bookmarks WHERE id = ?", (bid,),
            )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_pinned_list(self, qs=None):
        # Scope the loose Pinned list to the toggled side so a Claude pin does not
        # linger in the ChatGPT sidebar (and vice versa).
        qs = qs or {}
        provider = query_rules.provider_from_qs(qs)
        prov_sql = query_rules.and_provider(provider)
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT p.conversation_id, p.pinned_at, p.order_index, "
                f"{DISPLAY_TITLE} AS title, c.update_time, c.message_count, c.preview "
                "FROM udb.pinned_conversations p "
                "JOIN conversations c ON c.id = p.conversation_id "
                f"{META_JOIN} "
                f"WHERE {NOT_DELETED}" + prov_sql + " "
                "ORDER BY p.order_index ASC, p.pinned_at DESC"
            ).fetchall()
            pinned = [dict(r) for r in rows]
            cids = [p["conversation_id"] for p in pinned]
            has_sum = set()
            if cids:
                marks = ",".join("?" * len(cids))
                for r in conn.execute(
                    "SELECT conversation_id FROM udb.conversation_summaries "
                    f"WHERE conversation_id IN ({marks}) "
                    "AND condensed_summary IS NOT NULL AND TRIM(condensed_summary) != ''",
                    tuple(cids),
                ).fetchall():
                    has_sum.add(r["conversation_id"])
            for p in pinned:
                p["has_condensed_summary"] = p["conversation_id"] in has_sum
            self.send_json({"pinned": pinned})
        finally:
            conn.close()

    def _api_pinned_add(self):
        payload = self._read_json_body()
        conv_id = (payload.get("conversation_id") or "").strip()
        if not conv_id:
            self.send_json({"error": "missing conversation_id"}, 400); return
        conn = open_db(self.db_path)
        try:
            exists = conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not exists:
                self.send_json({"error": "conversation not found"}, 404); return
            next_idx = conn.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM udb.pinned_conversations").fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO udb.pinned_conversations(conversation_id, pinned_at, order_index) VALUES (?, ?, ?)",
                (conv_id, time.time(), int(next_idx)),
            )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_pinned_remove(self, conv_id):
        conn = open_db(self.db_path)
        try:
            conn.execute("DELETE FROM udb.pinned_conversations WHERE conversation_id = ?", (conv_id,))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_pinned_reorder(self):
        payload = self._read_json_body()
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            self.send_json({"error": "ids must be a list"}, 400); return
        conn = open_db(self.db_path)
        try:
            for i, cid in enumerate(ids):
                conn.execute(
                    "UPDATE udb.pinned_conversations SET order_index = ? WHERE conversation_id = ?",
                    (i, str(cid)),
                )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_tabs(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                # No pinned DESC: the tab strip has no pin control any more, so
                # honouring the flag would strand a database written by an
                # older version with some tabs permanently jumping the queue
                # and no way to release them.
                "SELECT id, tab_type, conversation_id, artifact_id, title, pinned, sort_index, last_active_at, provider "
                "FROM udb.workspace_tabs WHERE closed = 0 AND compare = 1 "
                "ORDER BY sort_index ASC, last_active_at DESC"
            ).fetchall()
            self.send_json({"tabs": [dict(r) for r in rows]})
        finally:
            conn.close()

    def _api_tabs_create(self):
        payload = self._read_json_body()
        tab_id = (payload.get("id") or str(uuid.uuid4())).strip()
        tab_type = (payload.get("tab_type") or "conversation").strip()
        try:
            sort_index = int(payload["sort_index"]) if "sort_index" in payload else None
            last_active = float(payload.get("last_active_at", time.time()))
        except (TypeError, ValueError):
            raise ApiError("sort_index and last_active_at must be numbers.", 400)
        conn = open_db(self.db_path)
        try:
            next_idx = conn.execute("SELECT COALESCE(MAX(sort_index), -1) + 1 FROM udb.workspace_tabs WHERE closed = 0").fetchone()[0]
            provider = query_rules.provider_or_none(payload.get("provider"))
            conn.execute(
                "INSERT OR REPLACE INTO udb.workspace_tabs(id, tab_type, conversation_id, artifact_id, title, pinned, sort_index, last_active_at, closed, provider, compare) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1)",
                (
                    tab_id,
                    tab_type,
                    payload.get("conversation_id"),
                    payload.get("artifact_id"),
                    payload.get("title") or "",
                    1 if payload.get("pinned") else 0,
                    next_idx if sort_index is None else sort_index,
                    last_active,
                    provider,
                ),
            )
            conn.commit()
            self.send_json({"ok": True, "id": tab_id})
        finally:
            conn.close()

    def _api_tab_update(self, tab_id):
        payload = self._read_json_body()
        if not tab_id:
            self.send_json({"error": "missing tab id"}, 400); return
        fields = []
        values = []
        for k in ("title", "conversation_id", "artifact_id", "sort_index", "tab_type"):
            if k in payload:
                fields.append(f"{k} = ?")
                values.append(payload[k])
        if "pinned" in payload:
            fields.append("pinned = ?")
            values.append(1 if payload.get("pinned") else 0)
        fields.append("last_active_at = ?")
        values.append(time.time())
        values.append(tab_id)
        conn = open_db(self.db_path)
        try:
            conn.execute(f"UPDATE udb.workspace_tabs SET {', '.join(fields)} WHERE id = ?", tuple(values))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_tab_remove(self, tab_id):
        conn = open_db(self.db_path)
        try:
            conn.execute("UPDATE udb.workspace_tabs SET closed = 1 WHERE id = ?", (tab_id,))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_conversation_update(self, conv_id):
        payload = self._read_json_body()
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        rename = payload.get("title")
        if isinstance(rename, str):
            check_text("Title", rename.strip(), request_limits.CONVERSATION_TITLE_MAX)
        archive = payload.get("archived")
        deleted = payload.get("deleted")
        conn = open_db(self.db_path)
        try:
            exists = conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not exists:
                self.send_json({"error": "conversation not found"}, 404); return
            conn.execute(
                "INSERT OR IGNORE INTO udb.conversation_meta(conversation_id, custom_title, archived, deleted) VALUES (?, NULL, 0, 0)",
                (conv_id,),
            )
            if isinstance(rename, str):
                cleaned_title = rename.strip() or None
                conn.execute(
                    "UPDATE udb.conversation_meta SET custom_title = ? WHERE conversation_id = ?",
                    (cleaned_title, conv_id),
                )
            if archive is not None:
                conn.execute(
                    "UPDATE udb.conversation_meta SET archived = ? WHERE conversation_id = ?",
                    (1 if archive else 0, conv_id),
                )
                # Archiving a foldered chat removes it from its folder (a chat
                # cannot be both archived and in a folder).
                if archive:
                    conn.execute(
                        "DELETE FROM udb.folder_items WHERE conversation_id = ?",
                        (conv_id,),
                    )
            if deleted is not None:
                conn.execute(
                    "UPDATE udb.conversation_meta SET deleted = ? WHERE conversation_id = ?",
                    (1 if deleted else 0, conv_id),
                )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    # ── Recycle Bin ─────────────────────────────────────────────────────────────

    def _recycle_target_ids(self, conn, payload):
        """Resolve which deleted conversations a bulk action applies to.

        `all` selects every conversation currently in the Recycle Bin, scoped to
        the given provider side so it matches the count the user sees; otherwise
        the explicit `ids` list is used (filtered to actually-deleted rows)."""
        provider = query_rules.provider_or_none(payload.get("provider"))
        prov_sql = query_rules.and_provider(provider)
        if payload.get("all"):
            rows = conn.execute(
                "SELECT c.id FROM conversations c "
                f"{META_JOIN} "
                f"WHERE {IN_RECYCLE_BIN}" + prov_sql
            ).fetchall()
            return [r[0] for r in rows]
        ids = payload.get("ids") or []
        ids = [str(i) for i in ids if i]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT c.id FROM conversations c "
            f"{META_JOIN} "
            f"WHERE {IN_RECYCLE_BIN} AND c.id IN ({marks})" + prov_sql,
            tuple(ids),
        ).fetchall()
        return [r[0] for r in rows]

    def _api_recycle_restore(self):
        """Restore conversations out of the Recycle Bin: clear the deleted flag
        while leaving every other bit of metadata (folder, pins, tags, models)
        intact, so each returns to exactly the state it had before deletion."""
        payload = self._read_json_body()
        conn = open_db(self.db_path)
        try:
            ids = self._recycle_target_ids(conn, payload)
            if not ids:
                self.send_json({"ok": True, "restored": 0}); return
            marks = ",".join("?" * len(ids))
            # Single statement → the whole selection restores or none does.
            conn.execute(
                f"UPDATE udb.conversation_meta SET deleted = 0 "
                f"WHERE conversation_id IN ({marks})",
                tuple(ids),
            )
            conn.commit()
            self.send_json({"ok": True, "restored": len(ids)})
        except sqlite3.Error as e:
            conn.rollback()
            self._log_request_error(e, 500)
            self.send_json({"error": _busy_or_failed(e, "The restore couldn't finish. Nothing was restored.")}, 500)
        finally:
            conn.close()

    def _api_recycle_purge(self):
        """Permanently delete conversations from the Recycle Bin. Runs the whole
        batch inside one transaction so a mid-way failure leaves nothing
        half-deleted — either every selected conversation is gone (with a
        deletion record written for each) or the database is untouched."""
        payload = self._read_json_body()
        conn = open_db(self.db_path)
        try:
            ids = self._recycle_target_ids(conn, payload)
            if not ids:
                self.send_json({"ok": True, "purged": 0}); return
            purged = 0
            for cid in ids:
                if _purge_conversation(conn, cid):
                    purged += 1
            conn.commit()
            self.send_json({"ok": True, "purged": purged})
        except sqlite3.Error as e:
            conn.rollback()
            self._log_request_error(e, 500)
            self.send_json({"error": _busy_or_failed(e, "The permanent delete couldn't finish. Nothing was deleted.")}, 500)
        finally:
            conn.close()

    # ── Re-import review ─────────────────────────────────────────────────────────

    def _api_review_list(self, qs=None):
        """Conversations held aside from an import for the user to review before
        re-importing (the Review choice, and the has-new-messages skip case)."""
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT conversation_id, provider, chat_name, first_message, "
                "last_message, total_messages, reason, created_at "
                "FROM udb.pending_reimport ORDER BY created_at DESC, chat_name ASC"
            ).fetchall()
            self.send_json({"review": [dict(r) for r in rows]})
        except sqlite3.Error:
            self.send_json({"review": []})
        finally:
            conn.close()

    def _api_review_import(self):
        payload = self._read_json_body()
        db = Path(self.db_path)
        self.send_json({"job": background_jobs.start(db, "review-import",
            lambda job: review_import(db, payload, job))}, 202)


def review_import(db_path, payload, job):
    """Bring back each reviewed chat, clearing its tombstone only on success."""
    import build_db
    backups.backup_all(db_path, "before-import")
    def _norm(items):
        out = []
        for it in (items or []):
            if isinstance(it, dict):
                cid = str(it.get("id") or it.get("conversation_id") or "")
                prov = str(it.get("provider") or "")
            else:
                cid, prov = str(it), ""
            if cid:
                out.append((cid, prov))
        return out

    conn = open_db(db_path)
    try:
        if payload.get("import_all"):
            import_ids = [(r[0], r[1]) for r in conn.execute(
                "SELECT conversation_id, provider FROM udb.pending_reimport"
            ).fetchall()]
        else:
            import_ids = _norm(payload.get("import_ids"))
        if payload.get("dismiss_all"):
            dismiss_ids = [(r[0], r[1]) for r in conn.execute(
                "SELECT conversation_id, provider FROM udb.pending_reimport"
            ).fetchall()]
        else:
            dismiss_ids = _norm(payload.get("dismiss_ids"))

        imported = 0
        dismissed = 0
        completed = 0
        result = {"ok": True, "imported": 0, "dismissed": 0, "errors": 0}
        job.result = result
        job.progress("Bringing back reviewed chats…", done=0,
                     total=len(import_ids) + len(dismiss_ids), force=True)
        for cid, prov in import_ids:
            job.check()
            try:
                row = conn.execute(
                    "SELECT provider, record_json FROM udb.pending_reimport "
                    "WHERE conversation_id = ? AND (provider = ? OR ? = '')",
                    (cid, prov, prov),
                ).fetchone()
                if row is None:
                    continue
                provider = row["provider"]
                try:
                    record = json.loads(row["record_json"])
                except (ValueError, TypeError) as exc:
                    errorlog.record_exception(exc, action="Read reviewed chat", ids={"conversation": cid})
                    raise ApiError("A chat in the review list could not be read. It is still in the list.", 500)
                if record:
                    # Reuse the canonical reconcile path → same identity, no dup.
                    counts = build_db.reconcile_backup(conn, [record], provider)
                    if counts["errors"] or counts["skipped"]:
                        result["errors"] += 1
                        continue
                else:
                    raise ApiError("A chat in the review list is empty. It is still in the list.", 500)
                # No longer deleted once re-imported.
                conn.execute(
                    "DELETE FROM udb.deleted_records "
                    "WHERE conversation_id = ? AND provider = ?",
                    (cid, provider),
                )
                conn.execute(
                    "DELETE FROM udb.pending_reimport "
                    "WHERE conversation_id = ? AND provider = ?",
                    (cid, provider),
                )
                imported += 1
                result["imported"] = imported
            finally:
                completed += 1
                job.state['progress'] = completed
                job.batch(conn, completed)

        for cid, prov in dismiss_ids:
            job.check()
            cur = conn.execute(
                "DELETE FROM udb.pending_reimport "
                "WHERE conversation_id = ? AND (provider = ? OR ? = '')",
                (cid, prov, prov),
            )
            dismissed += cur.rowcount or 0
            result["dismissed"] = dismissed
            completed += 1
            job.state['progress'] = completed
            job.batch(conn, completed)

        conn.commit()
        return result
    except background_jobs.Cancelled:
        conn.commit()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


for _name in ("do_GET", "do_POST", "do_PATCH", "do_PUT", "do_DELETE"):
    if hasattr(Handler, _name):
        setattr(Handler, _name, _log_errors(getattr(Handler, _name)))


def _bind_server(preferred_port, quick_attempts=6, delay=0.4):
    """Bind the HTTP server and return (httpd, actual_port).

    Prefer `preferred_port`, briefly retrying to ride out a port a just-closed
    instance is still releasing (e.g. our own auto-restart, which frees and
    re-takes the same port). If it stays busy, another copy the user simply
    forgot to close is holding it — so instead of refusing to start, fall back
    to any free port and run there. A forgotten old instance never blocks a new
    one; the new one always comes up cleanly on some port."""
    # SO_REUSEADDR lets us re-bind while the old socket lingers in TIME_WAIT.
    ThreadingHTTPServer.allow_reuse_address = True
    # Phase 1: try to get the preferred port, tolerating a brief busy window.
    for attempt in range(1, quick_attempts + 1):
        try:
            return ThreadingHTTPServer(("127.0.0.1", preferred_port), Handler), preferred_port
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                raise
            if attempt < quick_attempts:
                if attempt == 1:
                    print(f"Port {preferred_port} is busy — waiting briefly in "
                          f"case a previous instance is still closing…",
                          flush=True)
                time.sleep(delay)
    # Phase 2: still busy — a copy the user left open is holding it. Take a free
    # port (0 lets the OS pick one atomically) so this instance runs cleanly.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    actual = httpd.server_address[1]
    print(f"Port {preferred_port} is in use by another copy that's still open; "
          f"starting this instance on port {actual} instead.", flush=True)
    return httpd, actual


def _can_restart() -> bool:
    """Whether we can re-exec ourselves to load new code. True for a normal
    `python app.py` / `python server.py` launch; false for e.g. `python -c …`,
    where re-execing would not reproduce this server."""
    argv0 = sys.argv[0] if sys.argv else ""
    return bool(argv0) and argv0.endswith(".py") and Path(argv0).exists()


def _restart_process(actual_port) -> None:
    """Re-exec this process to load the new code on disk. The current listening
    socket is close-on-exec, so the port frees and the fresh process re-binds
    it; we pass the port through the environment so a fallback port is kept
    across the restart and the open browser tab reconnects to the same URL."""
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    os.environ["CHV_PORT"] = str(actual_port)
    try:
        if sys.platform == "win32":
            # A new process binds after this one's listening socket is closed.
            restart_env = os.environ.copy()
            restart_env["CHV_RESTART_WAIT_PORT"] = str(actual_port)
            subprocess.Popen([sys.executable] + sys.argv, env=restart_env, cwd=os.getcwd())
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        # Very unusual (e.g. a platform where re-exec is unavailable). Say so in
        # the terminal — where the user launched it — instead of failing mutely.
        print(f"Auto-reload could not restart the server ({e}). "
              f"Restart it manually to load the change.",
              file=sys.stderr, flush=True)


def _start_autoreload_watcher(actual_port, poll=1.0) -> None:
    """Watch the backend code files; when they change on disk, re-exec so the
    running server is never older than the code. A front-end-only change needs
    no restart — the page reloads itself when it sees the static files change
    (see /api/version)."""
    if not _can_restart():
        return
    def watch():
        pending = None
        while True:
            time.sleep(poll)
            try:
                sig = _code_signature()
            except Exception:
                continue
            if sig == _STARTUP_CODE_SIG:
                pending = None
                continue
            # An update replaces backend files before it keeps its backup and
            # replies; restart only once it has finished.
            if (updater.update_running(Path(__file__).resolve().parent)
                    or background_jobs.running(Handler.db_path)):
                pending = None
                continue
            # Require the new signature to be stable across two polls so we
            # don't re-exec on a half-written file mid-copy.
            if sig == pending:
                print("\nBackend code change detected — reloading the server…",
                      flush=True)
                _restart_process(actual_port)
                return
            pending = sig
    threading.Thread(target=watch, daemon=True).start()


def prepare_startup(db_path, source_dir):
    """A second copy starts without competing with the running job's writes.

    Version reads use SQLite WAL snapshots. Maintenance is deferred until a
    later idle start; every job must start with a current schema already.
    """
    try:
        with background_jobs.hold(db_path):
            _prepare_idle_startup(db_path, source_dir)
    except background_jobs.JobBusy:
        conn = open_db(db_path)
        try:
            versions = (schema_changes.get_version(conn), schema_changes.get_version(conn, "udb"))
            expected = (len(HISTORY_SCHEMA_STEPS), len(USERDATA_SCHEMA_STEPS))
            if versions != expected:
                raise schema_changes.SchemaChangeError(
                    "A background job is running with a different database version. "
                    "Use the same app version or wait for the job to finish.")
        finally:
            conn.close()
        backups.backup_all(db_path, "startup")


def _prepare_idle_startup(db_path, source_dir):
    # A copy of both databases on every start, before anything changes them.
    backups.backup_all(db_path, "startup")
    _ensure_runtime_schema(db_path)  # sets up userdata.db first
    _backfill_folder_providers(db_path)
    # Messages imported before export message ids were kept get them from the
    # backups in source/, in the background so start-up is not delayed.
    # Finished before the server answers anything, so no link or bookmark is
    # ever made from a message whose id is still missing. Real work happens
    # once; later starts find nothing to do.
    message_ids.fill_missing_from_sources(db_path, source_dir)
    # Then every bookmark made before ids were kept learns its message's id
    # and fingerprint while its position still points at that message.
    conn = open_db(db_path)
    try:
        message_ids.bind_legacy_bookmarks(conn)
    except sqlite3.Error as exc:
        errorlog.record_exception(exc, action="Update saved bookmarks at startup")
    finally:
        conn.close()


def serve(port=8000, db_path=app_paths.DB_PATH, source_dir=app_paths.SOURCE_DIR,
          on_ready=None, autoreload=True):
    wait_port = os.environ.pop("CHV_RESTART_WAIT_PORT", None)
    if wait_port:
        # The old Windows process exits after spawning us. Wait for its socket
        # before binding, so a quick child cannot jump to a fallback port.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", int(wait_port)), timeout=0.2):
                    pass
            except OSError:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("The previous app copy did not release its port during restart.")
    errorlog.configure(db_path=db_path)
    prepare_startup(db_path, source_dir)
    _build_source_index(source_dir)
    Handler.db_path = db_path
    Handler.source_dir = source_dir
    # ThreadingHTTPServer: handle each request on its own thread so one slow or
    # idle connection (e.g. a browser preconnect socket, or an aborted fetch)
    # can never block every other request. This is the fix for the UI hanging
    # on "Loading…". daemon_threads lets the process exit cleanly on Ctrl-C.
    httpd, actual_port = _bind_server(port)
    httpd.daemon_threads = True
    if autoreload:
        _start_autoreload_watcher(actual_port)
    # The socket is bound and listening now, so callers can safely open a
    # browser — no racing a fixed timer against startup, and pointed at the
    # port we actually got (which may differ from the preferred one).
    if on_ready:
        try:
            on_ready(actual_port)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        # Release the port immediately so the next restart doesn't have to wait.
        httpd.server_close()

if __name__ == "__main__":
    serve(port=int(os.environ.get("CHV_PORT", "8000")))
