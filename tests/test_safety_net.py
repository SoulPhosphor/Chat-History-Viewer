"""Session 1 safety net (AUDIT.md #14, #18, #20, #21, #22, #30). Throwaway
folders and made-up data only."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import app_paths
import backups
import errorlog
import schema_changes
import server
from server import _ensure_runtime_schema, _ensure_userdata_schema, _userdata_path

ROOT = Path(__file__).parents[1]


class PathTests(unittest.TestCase):
    def test_paths_come_from_the_app_folder(self):
        self.assertEqual(app_paths.APP_DIR, ROOT.resolve())
        self.assertEqual(app_paths.DB_PATH, ROOT.resolve() / "history.db")
        self.assertEqual(app_paths.SOURCE_DIR, ROOT.resolve() / "source")
        self.assertEqual(server.Handler.db_path, app_paths.DB_PATH)

    def test_app_started_from_another_folder_looks_in_its_own_folder(self):
        with tempfile.TemporaryDirectory() as other:
            code = "import app; print(app.DB_PATH); print(app.SOURCE)"
            out = subprocess.run([sys.executable, "-c", code], cwd=other, capture_output=True,
                                 text=True, env={"PYTHONPATH": str(ROOT)}, check=True).stdout
        db, source = out.split()
        self.assertEqual(Path(db), ROOT.resolve() / "history.db")
        self.assertEqual(Path(source), ROOT.resolve() / "source" / "conversations.json")


class VersionTests(unittest.TestCase):
    def test_minimum_python_is_3_12(self):
        text = (ROOT / "app.py").read_text()
        self.assertIn("MIN_PYTHON = (3, 12)", text)
        self.assertIn("3.12+", (ROOT / "README.md").read_text())

    def test_old_python_gets_a_clear_message(self):
        # Run app.py's check with a faked older version.
        source = (ROOT / "app.py").read_text().split("import os", 1)[0]
        fake = "import sys\nsys.version_info = (3, 10, 0)\n" + source.replace("import sys\n", "", 1)
        result = subprocess.run([sys.executable, "-c", fake], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs Python 3.12 or newer; this is Python 3.10", result.stderr)

    def test_version_and_changelog_agree(self):
        from version import __version__
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertIn(f"## {__version__}", (ROOT / "CHANGELOG.md").read_text())


class SchemaChangeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "history.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_both_databases_record_their_version(self):
        _ensure_runtime_schema(self.db_path)
        _ensure_userdata_schema(self.db_path)
        for path, steps in ((self.db_path, server.HISTORY_SCHEMA_STEPS),
                            (_userdata_path(self.db_path), server.USERDATA_SCHEMA_STEPS)):
            conn = sqlite3.connect(path)
            self.assertEqual(schema_changes.get_version(conn), len(steps))
            conn.close()

    def test_steps_run_once_in_order(self):
        calls = []
        steps = [lambda c: calls.append(1), lambda c: calls.append(2)]
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(schema_changes.apply_steps(conn, "test.db", steps[:1]), 1)
        self.assertEqual(schema_changes.apply_steps(conn, "test.db", steps), 2)
        self.assertEqual(schema_changes.apply_steps(conn, "test.db", steps), 2)
        self.assertEqual(calls, [1, 2])
        conn.close()

    def test_a_failed_step_is_reported_and_not_recorded(self):
        def bad_step(conn):
            conn.execute("ALTER TABLE no_such_table ADD COLUMN x TEXT")
        conn = sqlite3.connect(self.db_path)
        with self.assertRaises(schema_changes.SchemaChangeError) as ctx:
            schema_changes.apply_steps(conn, "test.db", [lambda c: None, bad_step])
        self.assertIn("test.db change 2 (bad_step) failed", str(ctx.exception))
        self.assertEqual(schema_changes.get_version(conn), 1)
        conn.close()

    def test_a_newer_database_is_refused(self):
        conn = sqlite3.connect(self.db_path)
        schema_changes.apply_steps(conn, "test.db", [lambda c: None, lambda c: None])
        with self.assertRaises(schema_changes.SchemaChangeError):
            schema_changes.apply_steps(conn, "test.db", [lambda c: None])
        conn.close()

    def test_add_column_skips_existing_columns_only(self):
        conn = sqlite3.connect(self.db_path)
        self.assertTrue(schema_changes.add_column(conn, "conversations", "extra TEXT"))
        self.assertFalse(schema_changes.add_column(conn, "conversations", "extra TEXT"))
        self.assertFalse(schema_changes.add_column(conn, "missing_table", "x TEXT"))
        with self.assertRaises(sqlite3.Error):
            schema_changes.add_column(conn, "conversations", "bad COLUMN DEFINITION (")
        conn.close()

    def test_database_is_backed_up_before_a_change(self):
        _ensure_runtime_schema(self.db_path)
        copies = backups.list_backups(self.db_path.parent / "backups")
        self.assertTrue(any(p.name.startswith("history-") and "before-db-change" in p.name
                            for p in copies))
        # No change pending the second time: no new copy.
        _ensure_runtime_schema(self.db_path)
        self.assertEqual(len(backups.list_backups(self.db_path.parent / "backups")), len(copies))


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "history.db"
        for path in (self.db_path, self.root / "userdata.db"):
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES (?)", (path.name,))
            conn.commit()
            conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_both_databases_are_copied_and_readable(self):
        made = backups.backup_all(self.db_path, "startup")
        self.assertEqual(len(made), 2)
        for copy in made:
            conn = sqlite3.connect(copy)
            self.assertIn(conn.execute("SELECT v FROM t").fetchone()[0], ("history.db", "userdata.db"))
            conn.close()
            self.assertRegex(copy.name, r"^(history|userdata)-\d{8}-\d{6}-\d{6}-startup\.db$")

    def test_only_the_newest_copies_are_kept(self):
        for _ in range(backups.KEEP + 3):
            backups.backup_all(self.db_path, "before-import")
        copies = backups.list_backups(self.root / "backups")
        self.assertEqual(len([p for p in copies if p.name.startswith("history-")]), backups.KEEP)
        self.assertEqual(len([p for p in copies if p.name.startswith("userdata-")]), backups.KEEP)

    def test_folder_names_with_uri_characters_work(self):
        folder = self.root / "a#b?c%20"
        folder.mkdir()
        conn = sqlite3.connect(folder / "history.db")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('kept')")
        conn.commit()
        conn.close()
        copy = backups.backup_file(folder / "history.db", "startup")
        conn = sqlite3.connect(copy)
        self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "kept")
        conn.close()

    def test_missing_database_is_skipped(self):
        (self.root / "userdata.db").unlink()
        self.assertEqual(len(backups.backup_all(self.db_path, "startup")), 1)

    def test_handler_backs_up_before_bulk_changes_and_refuses_if_it_cannot(self):
        handler = object.__new__(server.Handler)
        handler.db_path = self.db_path
        handler.command, handler.path = "POST", "/api/labels/bulk-apply"
        sent = []
        handler.send_json = lambda body, status=200: sent.append((status, body))
        self.assertEqual(handler._backup_reason("/api/labels/bulk-apply"), "before-bulk-change")
        self.assertEqual(handler._backup_reason("/api/snapshots/abc/restore"), "before-restore")
        self.assertIsNone(handler._backup_reason("/api/import-new"))  # job backs up after admission
        self.assertIsNone(handler._backup_reason("/api/labels"))
        self.assertTrue(handler._backup_before("before-bulk-change"))
        self.assertEqual(len(backups.list_backups(self.root / "backups")), 2)
        with tempfile.TemporaryDirectory() as logdir:
            errorlog.configure(log_dir=logdir, db_path=self.db_path)
            try:
                original = backups.backup_all
                backups.backup_all = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
                try:
                    self.assertFalse(handler._backup_before("before-bulk-change"))
                finally:
                    backups.backup_all = original
                self.assertEqual(sent[-1][0], 500)
                self.assertEqual(errorlog.read_log()["groups"][0]["error"], "OSError")
            finally:
                errorlog.configure(log_dir=app_paths.LOG_DIR, db_path=app_paths.DB_PATH)


class ErrorLogTests(unittest.TestCase):
    SECRET = "my private chat text about something"

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "history.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()
        _ensure_runtime_schema(self.db_path)
        _ensure_userdata_schema(self.db_path)
        errorlog.configure(log_dir=self.root / "logs", db_path=self.db_path)

    def tearDown(self):
        errorlog.configure(log_dir=app_paths.LOG_DIR, db_path=app_paths.DB_PATH)
        self.tempdir.cleanup()

    def raise_in_request(self, cid="3f9c0d1e-1111-4222-8333-444455556666", title=None):
        handler = object.__new__(server.Handler)
        handler.command = "PATCH"
        handler.path = f"/api/conversation/{cid}?q={self.SECRET.replace(' ', '+')}"
        handler._logged_body = {"title": title or self.SECRET, "label_id": "a" * 32,
                                "nested": {"note": self.SECRET}}
        handler._sent_status = None
        handler._front_door_problem = lambda: None
        self.replies = []
        handler.send_json = lambda body, status=200: self.replies.append((status, body))

        secret = self.SECRET
        body = handler._logged_body

        def do_PATCH(handler):
            handler._logged_body = body
            raise ValueError(f"could not save '{secret}' for row 17")
        wrapped = server._log_errors(do_PATCH)
        wrapped(handler)
        # AUDIT.md #9: a plain-language JSON 500, never a dropped connection.
        self.assertEqual(self.replies[-1][0], 500)
        self.assertNotIn(secret, json.dumps(self.replies[-1][1]))

    def test_entry_has_every_field(self):
        self.raise_in_request()
        group = errorlog.read_log()["groups"][0]
        entry = group["recent"][0]
        self.assertRegex(entry["error_id"], r"^[0-9a-f-]{36}$")
        self.assertRegex(entry["when"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(entry["where"], "server")
        self.assertEqual(entry["error_type"], "ValueError")
        self.assertIn("for row 17", entry["message"])
        self.assertTrue(entry["traceback"])
        self.assertTrue({"file", "line", "function", "code"} <= set(entry["traceback"][-1]))
        self.assertEqual(entry["action"], "PATCH /api/conversation/<id>?q=…")
        self.assertEqual(entry["status"], 500)
        self.assertEqual(entry["ids"]["path"], ["3f9c0d1e-1111-4222-8333-444455556666"])
        self.assertEqual(entry["ids"]["label_id"], ["a" * 32])
        self.assertEqual(entry["request_shape"]["title"], f"{len(self.SECRET)} chars")
        self.assertEqual(entry["request_shape"]["nested"], {"note": f"{len(self.SECRET)} chars"})
        from version import __version__
        self.assertEqual(entry["app_version"], __version__)
        self.assertEqual(entry["db_versions"], {"history.db": len(server.HISTORY_SCHEMA_STEPS),
                                                "userdata.db": len(server.USERDATA_SCHEMA_STEPS)})
        self.assertTrue({"python", "sqlite", "os"} <= set(entry["system"]))

    def test_privacy_rule_no_text_in_the_file(self):
        self.raise_in_request()
        raw = (self.root / "logs" / "errors.json").read_text()
        for word in ("private", "chat text", "something"):
            self.assertNotIn(word, raw)

    def test_repeats_are_grouped_with_five_recent(self):
        for i in range(7):
            self.raise_in_request(cid=f"{i:032x}")
        groups = errorlog.read_log()["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 7)
        self.assertEqual(len(groups[0]["recent"]), errorlog.RECENT_PER_GROUP)
        self.assertLessEqual(groups[0]["first_seen"], groups[0]["last_seen"])
        self.assertEqual(groups[0]["recent"][-1]["ids"]["path"], [f"{6:032x}"])

    def test_at_most_200_groups_oldest_dropped(self):
        def name(i):  # letters only: numbers are ignored when grouping
            return "GET /api/" + "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))
        for i in range(errorlog.MAX_GROUPS + 5):
            errorlog.record(where="server", error_type="E", message="m", action=name(i))
        groups = errorlog.read_log()["groups"]
        self.assertEqual(len(groups), errorlog.MAX_GROUPS)
        actions = {g["action"] for g in groups}
        self.assertNotIn(name(0), actions)
        self.assertIn(name(errorlog.MAX_GROUPS + 4), actions)

    def test_browser_errors_keep_browser_fields(self):
        entry = errorlog.record(
            where="browser", error_type="TypeError", message="x is undefined",
            stack="renameConversation@http://127.0.0.1:5174/app.js:120:5",
            action="Rename conversation",
            browser={"name": "Firefox", "version": "142", "window": "1440×900"},
        )
        self.assertEqual(entry["browser"]["name"], "Firefox")
        self.assertEqual(entry["browser"]["window"], "1440×900")
        self.assertIn("app.js:120", entry["stack"])

    def test_a_broken_log_never_breaks_the_app(self):
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "errors.json").write_text("not json")
        self.assertIsNotNone(errorlog.record(where="server", error_type="E", message="m"))
        errorlog.configure(log_dir=self.root / "logs" / "errors.json" / "nope")
        self.assertIsNone(errorlog.record(where="server", error_type="E", message="m"))

    def test_scrub_keeps_names_and_drops_text(self):
        self.assertEqual(errorlog.scrub_message("no such column: 'title'"), "no such column: 'title'")
        self.assertEqual(errorlog.scrub_message('bad "two words"'), "bad <text: 9 chars>")


if __name__ == "__main__":
    unittest.main()
