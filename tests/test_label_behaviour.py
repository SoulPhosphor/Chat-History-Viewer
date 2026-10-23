"""Record how labels, bulk labels and snapshots behave today (AUDIT.md #26),
so later repair phases can't change them by accident. Throwaway databases
with made-up data only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import bulk_labels
import labels
import snapshots
from api_common import ApiError
from server import _ensure_runtime_schema, _ensure_userdata_schema, open_db


class LabelTestBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "history.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, "
            "create_time REAL, update_time REAL, provider TEXT)"
        )
        conn.executemany(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, 'claude')",
            [("c1", "One", 100, 100), ("c2", "Two", 200, 200), ("c3", "Three", 300, 300)],
        )
        conn.commit()
        conn.close()
        _ensure_runtime_schema(self.db_path)
        _ensure_userdata_schema(self.db_path)
        self.conn = open_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def make_label(self, name="", color="#ff0000"):
        return labels.create_label(self.conn, {"name": name, "color": color})["id"]

    def labels_of(self, cid):
        return [l["id"] for l in labels.conv_labels(self.conn, cid)]


class LabelTests(LabelTestBase):
    def test_label_name_is_optional(self):
        lid = self.make_label("")
        row = [l for l in labels.list_labels(self.conn)["labels"] if l["id"] == lid][0]
        self.assertEqual(row["name"], "")

    def test_new_labels_go_to_the_end_of_the_order(self):
        a, b = self.make_label("A"), self.make_label("B")
        order = [l["id"] for l in labels.list_labels(self.conn)["labels"]]
        self.assertEqual(order, [a, b])

    def test_reorder_sets_the_order(self):
        a, b, c = self.make_label("A"), self.make_label("B"), self.make_label("C")
        labels.reorder_labels(self.conn, {"order": [c, a, b]})
        order = [l["id"] for l in labels.list_labels(self.conn)["labels"]]
        self.assertEqual(order, [c, a, b])

    def test_conversation_can_carry_several_labels(self):
        a, b = self.make_label("A"), self.make_label("B")
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a, "action": "add"})
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": b, "action": "add"})
        self.assertEqual(self.labels_of("c1"), [a, b])

    def test_toggle_only_remove_and_clear(self):
        a, b = self.make_label("A"), self.make_label("B")
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_ids": [a, b]})
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a, "action": "toggle"})
        self.assertEqual(self.labels_of("c1"), [b])
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a, "action": "only"})
        self.assertEqual(self.labels_of("c1"), [a])
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a, "action": "remove"})
        self.assertEqual(self.labels_of("c1"), [])
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_ids": [a, b]})
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": None})
        self.assertEqual(self.labels_of("c1"), [])

    def test_unknown_label_or_conversation_is_refused(self):
        with self.assertRaises(ApiError) as ctx:
            labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": "f" * 32})
        self.assertEqual(ctx.exception.status, 404)
        a = self.make_label("A")
        with self.assertRaises(ApiError) as ctx:
            labels.set_conv_label(self.conn, {"conv_id": "nope", "label_id": a})
        self.assertEqual(ctx.exception.status, 404)

    def test_deleting_a_label_blanks_its_conversations(self):
        a = self.make_label("A")
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a})
        result = labels.delete_label(self.conn, a)
        self.assertEqual(result["cleared"], 1)
        self.assertEqual(self.labels_of("c1"), [])


class BulkLabelTests(LabelTestBase):
    def test_preview_never_changes_anything(self):
        a = self.make_label("A")
        result = bulk_labels.bulk_preview(self.conn, {"label_action": "added", "label_targets": [a]})
        self.assertEqual((result["eligible"], result["will_change"]), (3, 3))
        self.assertEqual(self.labels_of("c1"), [])

    def test_apply_works_without_preview_and_logs_history(self):
        a = self.make_label("A")
        result = bulk_labels.bulk_apply(self.conn, {"label_action": "added", "label_targets": [a]})
        self.assertTrue(result["run_complete"])
        self.assertEqual(result["changed"], 3)
        for cid in ("c1", "c2", "c3"):
            self.assertEqual(self.labels_of(cid), [a])
        self.assertEqual(len(bulk_labels.bulk_history(self.conn)["history"]), 1)

    def test_all_matching_skips_conversations_already_done(self):
        a = self.make_label("A")
        labels.set_conv_label(self.conn, {"conv_id": "c2", "label_id": a})
        result = bulk_labels.bulk_apply(self.conn, {"label_action": "added", "label_targets": [a]})
        self.assertEqual((result["eligible"], result["changed"]), (3, 2))

    def test_first_n_saves_an_unfinished_run_and_continue_finishes_it(self):
        a = self.make_label("A")
        crit = {"label_action": "added", "label_targets": [a],
                "limit": {"mode": "first", "n": 2}, "order": "started_asc"}
        result = bulk_labels.bulk_apply(self.conn, crit)
        self.assertFalse(result["run_complete"])
        self.assertEqual(self.labels_of("c1"), [a])
        self.assertEqual(self.labels_of("c3"), [])
        runs = bulk_labels.bulk_run_list(self.conn)["runs"]
        self.assertEqual(len(runs), 1)
        bulk_labels.bulk_run_continue(self.conn, {"id": result["run_id"]})
        self.assertEqual(bulk_labels.bulk_run_list(self.conn)["runs"], [])
        self.assertEqual(self.labels_of("c3"), [a])

    def test_cleared_removes_every_label(self):
        a, b = self.make_label("A"), self.make_label("B")
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_ids": [a, b]})
        bulk_labels.bulk_apply(self.conn, {"label_action": "cleared"})
        self.assertEqual(self.labels_of("c1"), [])

    def test_added_without_a_target_is_refused(self):
        with self.assertRaises(ApiError) as ctx:
            bulk_labels.bulk_apply(self.conn, {"label_action": "added", "label_targets": []})
        self.assertEqual(ctx.exception.status, 400)


class SnapshotTests(LabelTestBase):
    def test_restore_returns_labels_to_the_snapshot(self):
        a = self.make_label("A")
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a})
        snapshots.snapshot_create(self.conn, {"name": "before"})
        sid = snapshots.snapshot_list(self.conn)["snapshots"][0]["id"]
        labels.delete_label(self.conn, a)
        self.make_label("B")
        result = snapshots.snapshot_restore(self.conn, sid, {"categories": ["labels"]})
        self.assertEqual(result["restored"], ["labels"])
        self.assertEqual(self.labels_of("c1"), [a])
        names = [l["name"] for l in labels.list_labels(self.conn)["labels"]]
        self.assertEqual(names, ["A"])

    def test_restore_skips_conversations_that_are_gone(self):
        a = self.make_label("A")
        labels.set_conv_label(self.conn, {"conv_id": "c1", "label_id": a})
        snapshots.snapshot_create(self.conn, {})
        sid = snapshots.snapshot_list(self.conn)["snapshots"][0]["id"]
        self.conn.execute("DELETE FROM conversations WHERE id = 'c1'")
        self.conn.commit()
        result = snapshots.snapshot_restore(self.conn, sid, {"categories": ["labels"]})
        self.assertEqual(result["skipped"], 1)

    def test_snapshot_limit_never_deletes_the_oldest(self):
        for i in range(snapshots.SNAPSHOT_MAX):
            snapshots.snapshot_create(self.conn, {"name": f"s{i}"})
        with self.assertRaises(ApiError) as ctx:
            snapshots.snapshot_create(self.conn, {"name": "one too many"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(len(snapshots.snapshot_list(self.conn)["snapshots"]), snapshots.SNAPSHOT_MAX)

    def test_restore_needs_a_category(self):
        snapshots.snapshot_create(self.conn, {})
        sid = snapshots.snapshot_list(self.conn)["snapshots"][0]["id"]
        with self.assertRaises(ApiError):
            snapshots.snapshot_restore(self.conn, sid, {"categories": []})


if __name__ == "__main__":
    unittest.main()
