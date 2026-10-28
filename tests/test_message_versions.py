"""Message versions: every reroll keeps its export id, the user's one edit per
version, hidden versions and sticky notes are kept by fixed ids, survive a
re-import, and links can point at a single version or edit. Throwaway folders
with made-up data only."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import advanced_search
import build_db
import conversation_tables
import message_ids
import message_versions
import scratchpads
import server
from api_common import ApiError
from server import _ensure_runtime_schema, _ensure_userdata_schema, open_db


def m(uid, sender, parent, text, second):
    return {"uuid": uid, "sender": sender, "parent_message_uuid": parent,
            "created_at": f"2024-01-01T00:00:{second:02d}Z",
            "content": [{"type": "text", "text": text}], "attachments": [], "files": []}


def reroll_chat(cid="c1"):
    """Three answers to the first question, two wordings of the second."""
    return {"uuid": cid, "name": "Reroll chat", "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z", "chat_messages": [
                m("h0", "human", None, "Question 0", 1),
                m("a0a", "assistant", "h0", "Answer first", 2),
                m("a0b", "assistant", "h0", "Answer second", 3),
                m("a0c", "assistant", "h0", "Answer third", 4),
                m("h1a", "human", "a0c", "Question 1 first wording", 5),
                m("a1a", "assistant", "h1a", "Reply to first wording", 6),
                m("h1b", "human", "a0c", "Question 1 second wording", 7),
                m("a1b", "assistant", "h1b", "Reply to second wording", 8),
            ]}


class MessageVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.export = self.source / "conversations.json"
        self.export.write_text(json.dumps([reroll_chat()]))
        self.db = self.root / "history.db"
        build_db.build(self.export, self.db)
        _ensure_runtime_schema(self.db)
        _ensure_userdata_schema(self.db)
        self.conn = open_db(self.db)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def siblings(self, seq):
        raw = self.conn.execute(
            "SELECT siblings FROM messages WHERE conversation_id = 'c1' AND seq = ?", (seq,)
        ).fetchone()[0]
        return json.loads(raw)

    def reimport(self):
        self.conn.close()
        build_db.build(self.export, self.db)
        _ensure_runtime_schema(self.db)
        _ensure_userdata_schema(self.db)
        self.conn = open_db(self.db)

    def test_every_reroll_keeps_its_export_id(self):
        self.assertEqual([s["uuid"] for s in self.siblings(1)], ["a0a", "a0b", "a0c"])
        prompts = self.siblings(2)
        self.assertEqual([s["uuid"] for s in prompts], ["h1a", "h1b"])
        self.assertEqual([s["asst_uuid"] for s in prompts], ["a1a", "a1b"])
        versions = message_versions.conversation_versions(self.conn, "c1")
        self.assertEqual(versions["a0b"], {"seq": 1, "index": 1})
        self.assertEqual(versions["a1a"], {"seq": 2, "index": 0, "reply": True})
        # The reply kept with the active wording is its own row.
        self.assertNotIn("reply", versions["a1b"])

    def test_older_database_gets_reroll_ids_from_the_backups(self):
        for seq in (1, 2):
            stripped = [{k: v for k, v in s.items() if k not in ("uuid", "asst_uuid")}
                        for s in self.siblings(seq)]
            self.conn.execute("UPDATE messages SET siblings = ? WHERE conversation_id = 'c1' AND seq = ?",
                              (json.dumps(stripped), seq))
        self.conn.commit()
        message_ids.fill_missing_from_sources(self.db, self.source)
        self.assertEqual([s["uuid"] for s in self.siblings(1)], ["a0a", "a0b", "a0c"])
        self.assertEqual([s["asst_uuid"] for s in self.siblings(2)], ["a1a", "a1b"])

    def test_reroll_ids_are_never_taken_from_different_versions(self):
        changed = self.siblings(1)
        for s in changed:
            s.pop("uuid")
        changed[0]["content"] = "Something else"
        self.conn.execute("UPDATE messages SET siblings = ? WHERE conversation_id = 'c1' AND seq = 1",
                          (json.dumps(changed),))
        self.conn.commit()
        message_ids.fill_missing_from_sources(self.db, self.source)
        self.assertNotIn("uuid", self.siblings(1)[0])

    def test_one_edit_per_version_and_the_original_stays(self):
        first = message_versions.save_edit(self.conn, {
            "conversation_id": "c1", "version_uuid": "a0b", "content": "My edit"})["edit"]
        again = message_versions.save_edit(self.conn, {
            "conversation_id": "c1", "version_uuid": "a0b", "content": "My second edit"})["edit"]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(message_versions.conversation_edits(self.conn, "c1")), 1)
        self.assertEqual(self.siblings(1)[1]["content"], "Answer second")
        with self.assertRaises(ApiError):
            message_versions.save_edit(self.conn, {"conversation_id": "c1", "version_uuid": "nope", "content": "x"})
        with self.assertRaises(ApiError):
            message_versions.save_edit(self.conn, {"conversation_id": "c1", "version_uuid": "a0b", "content": "  "})

    def test_hide_and_restore_one_item_only(self):
        edit = message_versions.save_edit(self.conn, {
            "conversation_id": "c1", "version_uuid": "a0c", "content": "Edited"})["edit"]
        message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": "a0a", "hidden": True})
        message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": edit["id"], "hidden": True})
        self.assertEqual(message_versions.conversation_hidden(self.conn, "c1"), ["a0a", edit["id"]])
        message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": "a0a", "hidden": False})
        self.assertEqual(message_versions.conversation_hidden(self.conn, "c1"), [edit["id"]])
        with self.assertRaises(ApiError):
            message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": "gone", "hidden": True})

    def test_reimport_never_undoes_edits_hiding_or_notes(self):
        edit = message_versions.save_edit(self.conn, {
            "conversation_id": "c1", "version_uuid": "a0a", "content": "Edited"})["edit"]
        message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": "a0b", "hidden": True})
        note = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 1})["note"]
        self.reimport()
        extras = message_versions.conversation_extras(self.conn, "c1")
        self.assertEqual([e["id"] for e in extras["message_edits"]], [edit["id"]])
        self.assertEqual(extras["message_edits"][0]["content"], "Edited")
        self.assertEqual(extras["hidden_items"], ["a0b"])
        self.assertEqual([n["id"] for n in extras["sticky_notes"]], [note["id"]])

    def test_one_sticky_note_per_message_with_untitled_numbering(self):
        first = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 0})
        self.assertTrue(first["created"])
        self.assertEqual(first["note"]["title"], "Untitled")
        same = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 0})
        self.assertFalse(same["created"])
        self.assertEqual(same["note"]["id"], first["note"]["id"])
        second = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 1})["note"]
        third = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 2})["note"]
        self.assertEqual((second["title"], third["title"]), ("Untitled 1", "Untitled 2"))

    def test_sticky_note_id_never_changes_and_old_saves_are_refused(self):
        note = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 0})["note"]
        renamed = message_versions.update_note(self.conn, note["id"], {"title": "  My   note "})["note"]
        self.assertEqual((renamed["id"], renamed["title"]), (note["id"], "My note"))
        saved = message_versions.update_note(self.conn, note["id"], {"body": "**hi**", "base_version": 0})["note"]
        self.assertEqual((saved["id"], saved["body"], saved["version"]), (note["id"], "**hi**", 1))
        with self.assertRaises(ApiError) as caught:
            message_versions.update_note(self.conn, note["id"], {"body": "older", "base_version": 0})
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ApiError):
            message_versions.update_note(self.conn, note["id"], {"title": "   "})

    def test_links_open_a_single_version_or_edit(self):
        edit = message_versions.save_edit(self.conn, {
            "conversation_id": "c1", "version_uuid": "a0b", "content": "Edited"})["edit"]
        reroll = scratchpads.resolve_link(self.conn, "c1", "1", "a0a")
        self.assertEqual((reroll["seq"], reroll["version"]["index"]), (1, 0))
        reply = scratchpads.resolve_link(self.conn, "c1", "3", "a1a")
        self.assertEqual((reply["seq"], reply["version"]["reply"]), (2, True))
        edited = scratchpads.resolve_link(self.conn, "c1", "1", edit["id"])
        self.assertEqual((edited["seq"], edited["version"]["edit_id"]), (1, edit["id"]))
        gone = scratchpads.resolve_link(self.conn, "c1", "1", "not-there")
        self.assertFalse(gone["message_exists"])

    def test_sticky_notes_in_advanced_search(self):
        note = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 1})["note"]
        message_versions.update_note(self.conn, note["id"], {"title": "Pelican", "body": "about birds"})
        hits = advanced_search.search(self.conn, {"query": "birds", "search_in": {"include": ["sticky_notes"]}})
        self.assertEqual([r["id"] for r in hits["results"]], ["c1"])
        self.assertEqual(hits["results"][0]["snippet_source"], "Sticky Note")
        must = advanced_search.search(self.conn, {"query": "", "must_have": ["sticky_notes"]})
        self.assertEqual([r["id"] for r in must["results"]], ["c1"])

    def test_delete_sticky_note(self):
        note = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 1})["note"]
        message_versions.delete_note(self.conn, note["id"])
        self.assertEqual(message_versions.conversation_notes(self.conn, "c1"), [])
        with self.assertRaises(ApiError) as caught:
            message_versions.delete_note(self.conn, note["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_snapshots_keep_edits_hidden_messages_and_sticky_notes(self):
        import snapshots
        edit = message_versions.save_edit(self.conn, {
            "conversation_id": "c1", "version_uuid": "a0a", "content": "Edited"})["edit"]
        message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": "a0b", "hidden": True})
        note = message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 1})["note"]
        sid = snapshots.snapshot_create(self.conn, {"name": "Before"})["snapshots"][0]["id"]
        message_versions.save_edit(self.conn, {"conversation_id": "c1", "version_uuid": "a0a", "content": "Later"})
        message_versions.set_hidden(self.conn, {"conversation_id": "c1", "item_id": "a0b", "hidden": False})
        message_versions.delete_note(self.conn, note["id"])
        result = snapshots.snapshot_restore(self.conn, sid, {})
        for key in ("message_edits", "hidden_messages", "sticky_notes"):
            self.assertIn(key, result["restored"])
        extras = message_versions.conversation_extras(self.conn, "c1")
        self.assertEqual([(e["id"], e["content"]) for e in extras["message_edits"]], [(edit["id"], "Edited")])
        self.assertEqual(extras["hidden_items"], ["a0b"])
        self.assertEqual([n["id"] for n in extras["sticky_notes"]], [note["id"]])

    def test_permanent_delete_removes_them(self):
        for table in ("message_edits", "hidden_messages", "sticky_notes"):
            self.assertIn((table, "conversation_id", None), conversation_tables.USERDATA_TABLES)
        message_versions.create_note(self.conn, {"conversation_id": "c1", "seq": 1})
        server._purge_conversation(self.conn, "c1")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM udb.sticky_notes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
