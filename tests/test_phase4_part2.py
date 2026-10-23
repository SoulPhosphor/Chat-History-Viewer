"""Session 7 (AUDIT.md #5, #11, #34, #19 data items, #39): artifacts keyed by
(chat, name), tag and memory ids, model ids, a version check on Notes and
Summaries, protected preference keys, fuller snapshots, and the Compare bar
and review list in userdata.db. Throwaway databases with made-up data only."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import build_db
import schema_changes
import server
import snapshots
from api_common import ApiError
from server import Handler, _ensure_runtime_schema, _purge_conversation, _userdata_path, open_db
from tests.test_advanced_search import conversation
from tests.test_scratchpads import build_history
from tests.test_user_data_move import remove_history, tables


def handler(db_path, payload=None):
    h = object.__new__(Handler)
    h.db_path = db_path
    h._read_json_body = lambda: payload or {}
    h.response = None
    h.send_json = lambda body, status=200: setattr(h, "response", (status, body))
    return h


def artifact(conv_id, name, content):
    return {"id": name, "conv_id": conv_id, "msg_seq": 1, "title": name,
            "type": "text/html", "lang": None, "content": content}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.db_path = build_history(self.root, [
            conversation("a", "One", ["gpt-4o"]), conversation("b", "Two", ["gpt-4o"])])

    def tearDown(self):
        self.temp.cleanup()

    def sql(self, query, *params):
        conn = open_db(self.db_path)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def run_sql(self, script, db="history"):
        path = self.db_path if db == "history" else _userdata_path(self.db_path)
        conn = sqlite3.connect(path)
        conn.executescript(script)
        conn.commit()
        conn.close()


class MoveCompareAndReviewTests(Base):
    def test_new_database_keeps_them_in_userdata_only(self):
        self.assertFalse({"workspace_tabs", "pending_reimport"} & tables(self.db_path))
        self.assertTrue({"workspace_tabs", "pending_reimport"} <= tables(_userdata_path(self.db_path)))

    def test_older_database_moves_rows_and_a_rebuild_keeps_them(self):
        # history.db the way version 1.5.0 left it: change 2, both tables in it.
        self.run_sql("""
            CREATE TABLE workspace_tabs (id TEXT PRIMARY KEY, tab_type TEXT NOT NULL,
                conversation_id TEXT, artifact_id TEXT, title TEXT, pinned INTEGER DEFAULT 0,
                sort_index INTEGER DEFAULT 0, last_active_at REAL, closed INTEGER DEFAULT 0,
                provider TEXT, compare INTEGER DEFAULT 0);
            CREATE TABLE pending_reimport (conversation_id TEXT NOT NULL, provider TEXT NOT NULL,
                chat_name TEXT, first_message REAL, last_message REAL, total_messages INTEGER,
                record_json TEXT, reason TEXT, created_at REAL,
                PRIMARY KEY (conversation_id, provider));
            INSERT INTO workspace_tabs (id, tab_type, conversation_id, title, provider, compare)
                VALUES ('t1', 'conversation', 'a', 'One', 'chatgpt', 1);
            INSERT INTO pending_reimport (conversation_id, provider, chat_name, record_json)
                VALUES ('held', 'chatgpt', 'Held', '{}');
            UPDATE schema_version SET version = 2;
        """)
        _ensure_runtime_schema(self.db_path)
        self.assertFalse({"workspace_tabs", "pending_reimport"} & tables(self.db_path))
        self.assertEqual(self.sql("SELECT id FROM udb.workspace_tabs")[0][0], "t1")
        self.assertEqual(self.sql("SELECT chat_name FROM udb.pending_reimport")[0][0], "Held")

        remove_history(self.db_path)
        server.rebuild_history(self.db_path, self.root)
        self.assertEqual(self.sql("SELECT id FROM udb.workspace_tabs")[0][0], "t1")
        self.assertEqual(self.sql("SELECT chat_name FROM udb.pending_reimport")[0][0], "Held")

    def test_backups_are_made_before_the_change(self):
        self.run_sql("UPDATE schema_version SET version = 2;")
        _ensure_runtime_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(schema_changes.get_version(conn), len(server.HISTORY_SCHEMA_STEPS))
        conn.close()
        made = {p.name.split("-")[0] for p in (self.root / "backups").glob("*before-db-change*")}
        self.assertEqual(made, {"history", "userdata"})

    def test_purge_still_removes_both(self):
        conn = open_db(self.db_path)
        conn.execute("INSERT INTO udb.workspace_tabs (id, tab_type, conversation_id) VALUES ('t', 'conversation', 'a')")
        conn.execute("INSERT INTO udb.pending_reimport (conversation_id, provider) VALUES ('a', 'chatgpt')")
        conn.commit()
        self.assertTrue(_purge_conversation(conn, "a"))
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM udb.workspace_tabs").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM udb.pending_reimport").fetchone()[0], 0)
        conn.close()


class ArtifactKeyTests(Base):
    def add_artifacts(self):
        conn = open_db(self.db_path)
        for cid, text in (("a", "first chat's game"), ("b", "second chat's game")):
            conn.execute(
                "INSERT OR REPLACE INTO artifacts (id, conv_id, msg_seq, title, type, lang, content) "
                "VALUES (:id, :conv_id, :msg_seq, :title, :type, :lang, :content)",
                artifact(cid, "snake-game", text))
        conn.commit()
        conn.close()

    def test_same_name_in_two_chats_keeps_both(self):
        self.add_artifacts()
        self.assertEqual(len(self.sql("SELECT * FROM artifacts WHERE id = 'snake-game'")), 2)

    def test_lookup_needs_the_chat_and_finds_the_right_one(self):
        self.add_artifacts()
        h = handler(self.db_path)
        h._api_artifact("snake-game", {"conv": ["b"]})
        self.assertEqual(h.response[1]["content"], "second chat's game")
        with self.assertRaises(ApiError) as caught:
            h._api_artifact("snake-game", {})
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ApiError) as caught:
            h._api_artifact("snake-game", {"conv": ["nope"]})
        self.assertEqual(caught.exception.status, 404)

    def test_build_keeps_one_per_chat(self):
        rows = [artifact("a", "x", "1"), artifact("a", "x", "2"), artifact("b", "x", "3")]
        conn = sqlite3.connect(":memory:")
        conn.executescript(build_db.SCHEMA)
        seen, kept = set(), []
        for art in reversed(rows):
            if (art["conv_id"], art["id"]) not in seen:
                seen.add((art["conv_id"], art["id"]))
                kept.append(art)
        conn.executemany(
            "INSERT OR REPLACE INTO artifacts (id, conv_id, msg_seq, title, type, lang, content) "
            "VALUES (:id, :conv_id, :msg_seq, :title, :type, :lang, :content)", kept)
        self.assertEqual(sorted(conn.execute("SELECT conv_id, content FROM artifacts")),
                         [("a", "2"), ("b", "3")])

    def test_older_table_is_rebuilt_with_the_new_key(self):
        self.run_sql("""
            DROP TABLE artifacts;
            CREATE TABLE artifacts (id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, msg_seq INTEGER,
                title TEXT, type TEXT, lang TEXT, content TEXT);
            INSERT INTO artifacts VALUES ('snake-game', 'b', 1, 't', 'text/html', NULL, 'kept');
            UPDATE schema_version SET version = 3;
        """)
        _ensure_runtime_schema(self.db_path)
        keys = [r[1] for r in sorted(self.sql("PRAGMA table_info(artifacts)"), key=lambda r: r[5]) if r[5]]
        self.assertEqual(keys, ["conv_id", "id"])
        self.assertEqual(self.sql("SELECT content FROM artifacts")[0][0], "kept")


class IdTests(Base):
    def test_tags_get_one_id_per_name_and_kind(self):
        for cid in ("a", "b"):
            h = handler(self.db_path, {"conv_id": cid, "tag": "Work"})
            h._api_tag_add()
        handler(self.db_path, {"conv_id": "a", "tag": "Work"})._api_mood_tag_add()
        tag_ids = {r[0] for r in self.sql("SELECT tag_id FROM udb.conversation_tags")}
        mood_ids = {r[0] for r in self.sql("SELECT tag_id FROM udb.conversation_mood_tags")}
        self.assertEqual(len(tag_ids), 1)
        self.assertEqual(len(mood_ids), 1)
        self.assertNotEqual(tag_ids, mood_ids)
        self.assertIsNotNone(next(iter(tag_ids)))

    def test_older_tags_get_ids(self):
        self.run_sql("""
            INSERT INTO conversation_tags (conversation_id, tag) VALUES ('a', 'Old'), ('b', 'Old');
            INSERT INTO conversation_mood_tags (conversation_id, tag) VALUES ('a', 'Calm');
            UPDATE schema_version SET version = 1;
        """, db="userdata")
        _ensure_runtime_schema(self.db_path)
        rows = self.sql("SELECT DISTINCT tag_id FROM udb.conversation_tags")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][0])
        self.assertIsNotNone(self.sql("SELECT tag_id FROM udb.conversation_mood_tags")[0][0])

    def test_memory_id_is_the_same_after_a_rebuild(self):
        (self.root / "memories.json").write_text(json.dumps(
            [{"account_uuid": "acc", "conversations_memory": "Likes tea."}]), encoding="utf-8")
        remove_history(self.db_path)
        server.rebuild_history(self.db_path, self.root)
        first = self.sql("SELECT memory_id FROM memories")[0][0]
        remove_history(self.db_path)
        server.rebuild_history(self.db_path, self.root)
        self.assertEqual(self.sql("SELECT memory_id FROM memories")[0][0], first)
        self.assertEqual(first, build_db.memory_id("acc", "Likes tea."))

    def test_models_use_the_file_ids_and_reload_matches_by_id(self):
        ids = {r[0] for r in self.sql("SELECT id FROM udb.claude_models")}
        self.assertIn("claude-opus-5", ids)
        conn = open_db(self.db_path)
        conn.execute("UPDATE udb.claude_models SET name = 'My Opus' WHERE id = 'claude-opus-5'")
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM udb.claude_models").fetchone()[0]
        handler(self.db_path)._reload_claude_models_from_seed(conn)
        after = conn.execute("SELECT COUNT(*) FROM udb.claude_models").fetchone()[0]
        name = conn.execute("SELECT name FROM udb.claude_models WHERE id = 'claude-opus-5'").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)
        self.assertEqual(name, "My Opus")

    def test_older_random_model_ids_become_file_ids(self):
        self.run_sql("""
            UPDATE claude_model_periods SET model_id = 'random1' WHERE model_id = 'claude-opus-5';
            UPDATE claude_models SET id = 'random1' WHERE id = 'claude-opus-5';
            INSERT INTO conversation_models (conversation_id, model_id) VALUES ('a', 'random1');
            UPDATE schema_version SET version = 3;
        """, db="userdata")
        _ensure_runtime_schema(self.db_path)
        self.assertEqual(self.sql("SELECT model_id FROM udb.conversation_models")[0][0], "claude-opus-5")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM udb.claude_models WHERE id = 'random1'")[0][0], 0)
        self.assertGreater(self.sql(
            "SELECT COUNT(*) FROM udb.claude_model_periods WHERE model_id = 'claude-opus-5'")[0][0], 0)


class VersionCheckTests(Base):
    def test_notes_refuse_a_save_from_an_old_copy(self):
        h = handler(self.db_path)
        h._api_notes_get("a")
        self.assertEqual(h.response[1]["version"], 0)
        handler(self.db_path, {"user_notes": "first", "base_version": 0})._api_notes_update("a")
        with self.assertRaises(ApiError) as caught:
            handler(self.db_path, {"user_notes": "stale", "base_version": 0})._api_notes_update("a")
        self.assertEqual(caught.exception.status, 409)
        h = handler(self.db_path, {"user_notes": "second", "base_version": 1})
        h._api_notes_update("a")
        self.assertEqual(h.response[1]["version"], 2)
        self.assertEqual(self.sql("SELECT user_notes FROM udb.conversation_notes")[0][0], "second")
        # A save that names no version is not checked.
        handler(self.db_path, {"notes_to_ai": "x"})._api_notes_update("a")

    def test_summaries_refuse_a_save_from_an_old_copy(self):
        handler(self.db_path, {"summary": "first", "base_version": 0})._api_summary_update("a")
        with self.assertRaises(ApiError) as caught:
            handler(self.db_path, {"summary": "stale", "base_version": 0})._api_summary_update("a")
        self.assertEqual(caught.exception.status, 409)
        h = handler(self.db_path)
        h._api_summary_get("a")
        self.assertEqual((h.response[1]["summary"], h.response[1]["version"]), ("first", 1))

    def test_browser_sends_the_version(self):
        root = Path(__file__).parents[1] / "static"
        self.assertIn("body.base_version", (root / "notes.js").read_text())
        self.assertIn("body.base_version", (root / "summary.js").read_text())


class PreferenceAndSnapshotTests(Base):
    def test_internal_preference_keys_are_protected(self):
        handler(self.db_path, {"preferences": {"dataset_format": "claude", "theme": "dark"}})._api_preferences_update()
        stored = dict(self.sql("SELECT pref_key, pref_value FROM udb.ui_preferences"))
        self.assertEqual(json.loads(stored["dataset_format"]), "chatgpt")
        self.assertEqual(json.loads(stored["theme"]), "dark")
        h = handler(self.db_path)
        h._api_preferences()
        self.assertNotIn("dataset_format", h.response[1]["preferences"])

    def test_snapshot_covers_notes_pads_and_more(self):
        conn = open_db(self.db_path)
        conn.execute("INSERT INTO udb.conversation_notes (conversation_id, user_notes, version) VALUES ('a', 'kept', 3)")
        conn.execute("INSERT INTO udb.conversation_mood_tags (conversation_id, tag) VALUES ('a', 'Calm')")
        conn.execute("INSERT INTO udb.scratch_pads (id, title, content, created_at, updated_at) VALUES ('p', 'Pad', 'x', 1, 1)")
        conn.commit()
        snap = snapshots.snapshot_create(conn, {"name": "S"})["snapshots"][0]
        for key in ("notes", "mood_tags", "scratch_pads", "bookmarks", "summaries", "links", "model_tags"):
            self.assertIn(key, snap["summary"])
        conn.execute("UPDATE udb.conversation_notes SET user_notes = 'changed', version = 4")
        conn.execute("DELETE FROM udb.conversation_mood_tags")
        conn.execute("DELETE FROM udb.scratch_pads")
        conn.commit()
        snapshots.snapshot_restore(conn, snap["id"], {})
        self.assertEqual(conn.execute("SELECT user_notes, version FROM udb.conversation_notes").fetchone()[:],
                         ("kept", 8))
        self.assertIsNotNone(conn.execute("SELECT tag_id FROM udb.conversation_mood_tags").fetchone()[0])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM udb.scratch_pads").fetchone()[0], 1)
        conn.close()

    def test_restores_never_reuse_a_version(self):
        conn = open_db(self.db_path)
        conn.execute("INSERT INTO udb.conversation_notes (conversation_id, user_notes, version) VALUES ('a', 'n', 1)")
        conn.commit()
        full = snapshots.snapshot_create(conn, {"name": "Full"})["snapshots"][0]["id"]
        conn.execute("DELETE FROM udb.conversation_notes")
        conn.commit()
        empty = snapshots.snapshot_create(conn, {"name": "Empty"})["snapshots"][0]["id"]
        seen = []
        for sid in (full, empty, full):
            snapshots.snapshot_restore(conn, sid, {"categories": ["notes"]})
            row = conn.execute("SELECT version FROM udb.conversation_notes").fetchone()
            if row:
                seen.append(row[0])
        conn.close()
        self.assertEqual(len(seen), 2)
        self.assertLess(seen[0], seen[1])

    def test_older_snapshot_leaves_newer_categories_alone(self):
        conn = open_db(self.db_path)
        conn.execute("INSERT INTO udb.conversation_notes (conversation_id, user_notes) VALUES ('a', 'mine')")
        payload = {"version": 1, "categories": {}, "data": {
            "labels": {"definitions": [], "assignments": []},
            "folders": {"definitions": [], "memberships": []},
            "tags": [], "titles": [], "meta_state": [], "pins": []}}
        conn.execute("INSERT INTO udb.snapshots (id, name, created_at, schema_version, payload) "
                     "VALUES ('old', 'Old', 1, 1, ?)", (json.dumps(payload),))
        conn.commit()
        result = snapshots.snapshot_restore(conn, "old", {})
        self.assertNotIn("notes", result["restored"])
        self.assertEqual(conn.execute("SELECT user_notes FROM udb.conversation_notes").fetchone()[0], "mine")
        conn.close()


if __name__ == "__main__":
    unittest.main()
