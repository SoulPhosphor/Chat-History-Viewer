"""Session 6 (AUDIT.md #6 and #7): renames, Archive, Recycle Bin, pins and
preferences live in userdata.db, a rebuild of history.db reads every export
in source/ and keeps permanently deleted chats deleted, and permanent delete
uses one list of the tables that belong to a conversation. Throwaway
databases with made-up data only."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import backups
import conversation_tables
import schema_changes
import server
from server import _ensure_runtime_schema, _purge_conversation, _userdata_path, open_db
from tests.test_advanced_search import conversation
from tests.test_scratchpads import build_history

MOVED = ("conversation_meta", "pinned_conversations", "ui_preferences")


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def remove_history(db_path):
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_database_keeps_user_state_in_userdata_only(self):
        _, db_path = build_history(self.root, [conversation("a", "One", ["gpt-4o"])])
        self.assertFalse(set(MOVED) & tables(db_path))
        self.assertTrue(set(MOVED) <= tables(_userdata_path(db_path)))
        conn = open_db(db_path)
        fmt = conn.execute(
            "SELECT pref_value FROM udb.ui_preferences WHERE pref_key = 'dataset_format'").fetchone()[0]
        conn.close()
        self.assertEqual(json.loads(fmt), "chatgpt")

    def test_older_database_moves_its_rows_and_drops_its_copies(self):
        _, db_path = build_history(self.root, [conversation("a", "One", ["gpt-4o"]),
                                                conversation("b", "Two", ["gpt-4o"])])
        # Put history.db back the way version 1.4.0 left it: change 1, with
        # the user's state in its own tables.
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE conversation_meta (conversation_id TEXT PRIMARY KEY, custom_title TEXT,
                archived INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0);
            CREATE TABLE pinned_conversations (conversation_id TEXT PRIMARY KEY, pinned_at REAL,
                order_index INTEGER);
            CREATE TABLE ui_preferences (pref_key TEXT PRIMARY KEY, pref_value TEXT);
            INSERT INTO conversation_meta VALUES ('a', 'Renamed', 0, 0), ('b', NULL, 1, 1);
            INSERT INTO pinned_conversations VALUES ('a', 5, 0);
            INSERT INTO ui_preferences VALUES ('theme', '"dark"'), ('dataset_format', '"claude"');
            UPDATE schema_version SET version = 1;
        """)
        conn.commit()
        conn.close()
        udb = sqlite3.connect(_userdata_path(db_path))
        for table in MOVED:
            udb.execute(f"DELETE FROM {table}")
        udb.execute("INSERT INTO ui_preferences VALUES ('dataset_format', '\"chatgpt\"')")
        udb.commit()
        udb.close()

        _ensure_runtime_schema(db_path)

        self.assertFalse(set(MOVED) & tables(db_path))
        conn = open_db(db_path)
        self.assertEqual(schema_changes.get_version(conn), len(server.HISTORY_SCHEMA_STEPS))
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT conversation_id, custom_title, archived, deleted FROM udb.conversation_meta "
                "ORDER BY conversation_id")],
            [("a", "Renamed", 0, 0), ("b", None, 1, 1)])
        self.assertEqual([tuple(r) for r in conn.execute(
            "SELECT conversation_id, pinned_at, order_index FROM udb.pinned_conversations")],
            [("a", 5.0, 0)])
        prefs = dict(conn.execute("SELECT pref_key, pref_value FROM udb.ui_preferences").fetchall())
        conn.close()
        self.assertEqual(prefs["theme"], '"dark"')
        # A value userdata.db already holds is kept.
        self.assertEqual(prefs["dataset_format"], '"chatgpt"')
        # Both files were copied before the change.
        names = [p.name for p in backups.list_backups(self.root / "backups")]
        self.assertTrue(any(n.startswith("history-") and "before-db-change" in n for n in names))
        self.assertTrue(any(n.startswith("userdata-") and "before-db-change" in n for n in names))


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.db_path = build_history(self.root, [
            conversation(cid, title, ["gpt-4o"]) for cid, title in
            (("renamed", "Old name"), ("archived", "Archived"), ("binned", "Binned"),
             ("purged", "Purged"), ("plain", "Plain"))])
        # A backup brought in with Import New Chats (renamed on import).
        (self.root / "extra.json").write_text(
            json.dumps([conversation("later", "From a later export", ["gpt-4o"])]), encoding="utf-8")
        conn = open_db(self.db_path)
        summary = server.import_new_backups(conn, self.root)
        self.assertEqual(summary["added"], 1)
        conn.executemany(
            "INSERT INTO udb.conversation_meta(conversation_id, custom_title, archived, deleted) "
            "VALUES (?, ?, ?, ?)",
            [("renamed", "New name", 0, 0), ("archived", None, 1, 0), ("binned", None, 0, 1),
             ("purged", None, 0, 1)])
        conn.execute("INSERT INTO udb.pinned_conversations VALUES ('renamed', 1, 0)")
        conn.execute("INSERT INTO udb.ui_preferences VALUES ('sidebarWidth', '300')")
        self.assertTrue(_purge_conversation(conn, "purged"))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        conn = open_db(self.db_path)
        try:
            return {
                "ids": {r[0] for r in conn.execute("SELECT id FROM conversations")},
                "meta": {r[0]: tuple(r[1:]) for r in conn.execute(
                    "SELECT conversation_id, custom_title, archived, deleted FROM udb.conversation_meta")},
                "pins": [r[0] for r in conn.execute("SELECT conversation_id FROM udb.pinned_conversations")],
                "prefs": dict(conn.execute("SELECT pref_key, pref_value FROM udb.ui_preferences").fetchall()),
                "deleted_records": [r[0] for r in conn.execute(
                    "SELECT conversation_id FROM udb.deleted_records")],
                "messages_of_purged": conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = 'purged'").fetchone()[0],
            }
        finally:
            conn.close()

    def test_rebuild_keeps_user_state_reads_every_backup_and_honours_deletions(self):
        before = self.state()
        self.assertIn("later", before["ids"])
        self.assertNotIn("purged", before["ids"])
        remove_history(self.db_path)

        summary = server.rebuild_history(self.db_path, self.root)

        after = self.state()
        self.assertEqual(after, before)
        self.assertEqual(summary["seed"], "conversations.json")
        self.assertEqual(summary["kept_deleted"], 1)
        self.assertEqual(after["messages_of_purged"], 0)
        # The renamed backup kept its name and is in the import history again.
        conn = open_db(self.db_path)
        names = {r[0] for r in conn.execute("SELECT file_name FROM imported_backups")}
        conn.close()
        self.assertEqual(len(names), 2)
        self.assertIn("conversations.json", names)

    def test_rebuild_gives_a_binned_chat_its_newest_copy_and_keeps_it_binned(self):
        newer = conversation("binned", "Binned", ["gpt-4o", "gpt-4o"])
        newer["update_time"] = 50
        (self.root / "newer.json").write_text(json.dumps([newer]), encoding="utf-8")
        remove_history(self.db_path)
        server.rebuild_history(self.db_path, self.root)
        conn = open_db(self.db_path)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = 'binned'").fetchone()[0], 3)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM udb.conversation_meta WHERE conversation_id = 'binned'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_rebuild_without_conversations_json_starts_from_a_backup(self):
        self.source.unlink()
        remove_history(self.db_path)
        self.assertTrue(server.has_rebuild_source(self.root))
        summary = server.rebuild_history(self.db_path, self.root)
        self.assertNotEqual(summary["seed"], "conversations.json")
        self.assertIn("later", self.state()["ids"])

    def test_nothing_to_rebuild_from(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertFalse(server.has_rebuild_source(empty))
        with self.assertRaises(FileNotFoundError):
            server.rebuild_history(empty / "history.db", empty)


class PurgeListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.db_path = build_history(self.root, [
            conversation("gone", "Gone", ["gpt-4o"]), conversation("kept", "Kept", ["gpt-4o"])])
        self.conn = open_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def count(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_permanent_delete_removes_summaries_and_every_listed_row(self):
        c = self.conn
        for cid in ("gone", "kept"):
            c.execute("INSERT INTO udb.conversation_summaries(conversation_id, summary) VALUES (?, 'S')", (cid,))
            c.execute("INSERT INTO udb.conversation_meta(conversation_id, deleted) VALUES (?, 1)", (cid,))
            c.execute("INSERT INTO udb.pinned_conversations VALUES (?, 1, 0)", (cid,))
        c.execute("INSERT INTO udb.pending_reimport(conversation_id, provider) VALUES ('gone', 'chatgpt')")
        c.execute("INSERT INTO udb.pending_reimport(conversation_id, provider) VALUES ('gone', 'claude')")
        c.commit()
        self.assertTrue(_purge_conversation(c, "gone"))
        c.commit()
        for schema, entries in (("main", conversation_tables.HISTORY_TABLES),
                                ("udb", conversation_tables.USERDATA_TABLES)):
            for table, column, provider_column in entries:
                sql = f"SELECT COUNT(*) FROM {schema}.{table} WHERE {column} = 'gone'"
                if provider_column:
                    sql += f" AND {provider_column} = 'chatgpt'"
                self.assertEqual(self.count(sql), 0, table)
        self.assertEqual(self.count("SELECT COUNT(*) FROM conversations WHERE id = 'gone'"), 0)
        # The same id on the other side is a different chat and stays.
        self.assertEqual(self.count("SELECT COUNT(*) FROM udb.pending_reimport WHERE provider = 'claude'"), 1)
        # The other chat is untouched.
        self.assertEqual(self.count(
            "SELECT COUNT(*) FROM udb.conversation_summaries WHERE conversation_id = 'kept'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE conversation_id = 'kept'"), 2)

    def test_permanent_delete_takes_the_chat_out_of_unfinished_bulk_runs(self):
        c = self.conn
        c.execute(
            "INSERT INTO udb.label_bulk_runs(id, ids_json, completed_count, total_qualifying, batch_size) "
            "VALUES ('done-part', ?, 2, 3, 1), ('todo-part', ?, 1, 3, 1)",
            (json.dumps(["x", "gone", "y"]), json.dumps(["x", "gone", "y"])))
        c.commit()
        _purge_conversation(c, "gone")
        c.commit()
        runs = {r[0]: (json.loads(r[1]), r[2], r[3]) for r in c.execute(
            "SELECT id, ids_json, completed_count, total_qualifying FROM udb.label_bulk_runs")}
        # Already processed: one fewer done. Still to do: nothing done changes.
        self.assertEqual(runs["done-part"], (["x", "y"], 1, 2))
        self.assertEqual(runs["todo-part"], (["x", "y"], 1, 2))


if __name__ == "__main__":
    unittest.main()
