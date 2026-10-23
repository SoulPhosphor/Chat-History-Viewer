"""Session 8 (AUDIT.md Phase 5 part 1: #4, #12, #8, #31, #24): the importer
reads whole chats, keeps every attachment, counts the content it leaves out,
writes each chat completely or not at all, runs one import at a time, reads
exports one chat at a time, checks free disk space first, and Upload asks
before replacing a file. Throwaway folders with made-up data only."""
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import background_jobs
import tracemalloc
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import build_db
import errorlog
import import_lock
import server
from server import Handler, open_db
from tests.test_advanced_search import conversation
from tests.test_scratchpads import build_history


def claude_chat(cid, turns, *, steps=1, name="Long chat", attachments=None, extra_blocks=None):
    """A Claude export chat with `turns` question/answer turns. Each answer is
    `steps` assistant messages in a row (a tool-using reply)."""
    msgs, parent = [], None
    for t in range(turns):
        h = f"{cid}-h{t}"
        msgs.append({
            "uuid": h, "sender": "human", "parent_message_uuid": parent,
            "created_at": f"2024-01-01T00:{t // 60 % 60:02d}:{t % 60:02d}Z",
            "content": [{"type": "text", "text": f"Question {t}"}],
            "attachments": attachments if t == 0 and attachments else [],
            "files": [],
        })
        parent = h
        for s in range(steps):
            a = f"{cid}-a{t}-{s}"
            blocks = [{"type": "text", "text": f"Answer {t} step {s}"}]
            if t == 0 and s == 0 and extra_blocks:
                blocks = extra_blocks + blocks
            msgs.append({
                "uuid": a, "sender": "assistant", "parent_message_uuid": parent,
                "created_at": f"2024-01-01T00:{t // 60 % 60:02d}:{t % 60:02d}Z",
                "content": blocks, "attachments": [], "files": [],
            })
            parent = a
    return {"uuid": cid, "name": name, "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z", "chat_messages": msgs}


def write_export(path, chats):
    path.write_text(json.dumps(chats), encoding="utf-8")
    return path


# ── Today's behaviour, recorded before the changes ─────────────────────────────


class RecordedBehaviourTests(unittest.TestCase):
    """What the importer already did right; the session's changes keep it."""

    def test_extracted_attachment_is_a_chip_with_its_text(self):
        msg = {"sender": "human", "content": [{"type": "text", "text": "hi"}],
               "attachments": [{"file_name": "notes.txt", "file_type": "txt",
                                "extracted_content": "the notes"}],
               "files": [{"file_name": "notes.txt", "file_uuid": "u1"}]}
        text, atts = build_db.claude_message_parts(msg)
        self.assertEqual(text, "hi")
        # The files entry for the same name is not shown a second time.
        self.assertEqual(atts, [{"name": "notes.txt", "type": "txt", "content": "the notes"}])

    def test_file_without_text_is_a_chip_with_its_file_id(self):
        msg = {"sender": "human", "content": [], "attachments": [],
               "files": [{"file_name": "photo.png", "file_uuid": "u2"}]}
        _, atts = build_db.claude_message_parts(msg)
        self.assertEqual(atts, [{"name": "photo.png", "type": "png", "content": "", "uuid": "u2"}])

    def test_short_chat_imports_every_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = write_export(root / "conversations.json", [claude_chat("c1", 5, steps=2)])
            build_db.build(src, root / "history.db")
            conn = sqlite3.connect(root / "history.db")
            n, status = conn.execute(
                "SELECT message_count, import_status FROM conversations WHERE id = 'c1'").fetchone()
            conn.close()
            self.assertEqual((n, status), (10, "normal"))

    def test_build_collapses_duplicates_keeping_the_fuller_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = write_export(root / "conversations.json", [
                claude_chat("dup", 2), claude_chat("dup", 4), claude_chat("other", 1), "junk"])
            build_db.build(src, root / "history.db")
            conn = sqlite3.connect(root / "history.db")
            self.assertEqual(conn.execute(
                "SELECT message_count, source_index FROM conversations WHERE id = 'dup'").fetchone(), (8, 1))
            rows = conn.execute(
                "SELECT source_index, kept, import_status, message_count FROM import_sources "
                "ORDER BY source_index").fetchall()
            self.assertEqual(rows, [(0, 0, "normal", 4), (1, 1, "normal", 8),
                                    (2, 1, "normal", 2), (3, 1, "parse_error", 0)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 3)
            # The seed file is recorded so Import New Chats skips it.
            self.assertEqual(conn.execute(
                "SELECT file_name, total_chats FROM imported_backups").fetchall(),
                [("conversations.json", 3)])
            conn.close()

    def test_import_new_notes_files_it_cannot_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, db_path = build_history(root, [conversation("a", "One", ["gpt-4o"])])
            (root / "broken.json").write_text('[{"id": "x", ', encoding="utf-8")
            (root / "other.json").write_text('{"not": "a list"}', encoding="utf-8")
            write_export(root / "new.json", [conversation("b", "Two", ["gpt-4o"])])
            conn = open_db(db_path)
            summary = server.import_new_backups(conn, root)
            conn.commit()
            conn.close()
            self.assertEqual(summary["added"], 1)
            notes = " ".join(summary["notes"])
            self.assertIn("broken.json: not valid JSON", notes)
            self.assertIn("other.json: not a recognized Claude or ChatGPT export", notes)
            self.assertEqual(len(summary["files"]), 1)


# ── #4: long chats ─────────────────────────────────────────────────────────────


class LongChatTests(unittest.TestCase):
    def test_more_than_300_turns_are_kept(self):
        msgs, _ = build_db._claude_active_path({}, "c", claude_chat("c", 450)["chat_messages"])
        self.assertEqual(len(msgs), 900)
        rec = build_db.import_claude_conversation(claude_chat("c", 450), 0)
        self.assertEqual((rec["status"], rec["meta"]["message_count"]), ("normal", 900))

    def test_more_than_100_steps_in_one_answer_are_kept(self):
        msgs, _ = build_db._claude_active_path({}, "c", claude_chat("c", 2, steps=150)["chat_messages"])
        self.assertEqual(len(msgs), 4)
        self.assertIn("Answer 0 step 149", msgs[1]["content"])
        self.assertEqual(msgs[2]["content"], "Question 1")

    def test_a_loop_in_the_export_still_ends(self):
        chat = claude_chat("c", 3, steps=2)
        # The first assistant message points back at the last one: a cycle.
        chat["chat_messages"][1]["parent_message_uuid"] = chat["chat_messages"][-1]["uuid"]
        rec = build_db.import_claude_conversation(chat, 0)
        self.assertIn(rec["status"], ("normal", "fallback"))


# ── #12: attachments and content types ─────────────────────────────────────────


class AttachmentTests(unittest.TestCase):
    def test_attachment_without_text_is_kept_as_a_missing_file(self):
        msg = {"sender": "human", "content": [{"type": "text", "text": "see file"}],
               "attachments": [{"file_name": "report.pdf", "file_type": "pdf", "extracted_content": ""}],
               "files": []}
        _, atts = build_db.claude_message_parts(msg)
        self.assertEqual(atts, [{"name": "report.pdf", "type": "pdf", "content": ""}])

    def test_attachment_without_text_takes_the_file_id_of_the_same_file(self):
        msg = {"sender": "human", "content": [],
               "attachments": [{"file_name": "a.pdf", "file_type": "pdf"}],
               "files": [{"file_name": "a.pdf", "file_uuid": "u9"},
                         {"file_name": "b.png", "file_uuid": "u8"}]}
        _, atts = build_db.claude_message_parts(msg)
        self.assertEqual(atts, [{"name": "a.pdf", "type": "pdf", "content": "", "uuid": "u9"},
                                {"name": "b.png", "type": "png", "content": "", "uuid": "u8"}])

    def test_missing_attachment_shows_in_the_attachment_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = claude_chat("c", 1, attachments=[{"file_name": "lost.docx", "file_type": "docx"}])
            _, db_path = build_history(root, [chat])
            conn = open_db(db_path)
            atts = json.loads(conn.execute(
                "SELECT attachments FROM messages WHERE conversation_id = 'c' AND role = 'user'").fetchone()[0])
            conn.close()
            self.assertEqual(atts[0]["name"], "lost.docx")

    def test_claude_content_types_are_counted(self):
        chat = claude_chat("c", 1, extra_blocks=[
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "name": "web_search", "input": {}},
            {"type": "brand_new_block", "data": 1},
        ])
        rec = build_db.import_claude_conversation(chat, 0)
        self.assertEqual(rec["content_types"]["dropped"], {"thinking": 1, "tool_use": 1})
        self.assertEqual(rec["content_types"]["unknown"], {"brand_new_block": 1})

    def test_chatgpt_content_types_are_counted(self):
        conv = conversation("g", "GPT", ["gpt-4o"])
        node = next(iter(conv["mapping"].values()))
        node["message"]["content"] = {"content_type": "thoughts", "thoughts": []}
        conv["mapping"]["extra"] = {"id": "extra", "parent": None, "children": [], "message": {
            "author": {"role": "assistant"}, "content": {"content_type": "new_kind", "text": "x"}}}
        rec = build_db.import_chatgpt_conversation(conv, 0)
        self.assertEqual(rec["content_types"]["dropped"], {"thoughts": 1})
        self.assertEqual(rec["content_types"]["unknown"], {"new_kind": 1})

    def test_import_audit_shows_the_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = claude_chat("c", 1, extra_blocks=[{"type": "thinking"}, {"type": "mystery"}])
            _, db_path = build_history(root, [chat])
            write_export(root / "more.json", [claude_chat("d", 1, extra_blocks=[{"type": "thinking"}])])
            conn = open_db(db_path)
            server.import_new_backups(conn, root)
            conn.commit()
            conn.close()
            body = call_server(root, db_path, "GET", "/api/import-audit")[1]
            self.assertEqual(body["dropped_types"], {"thinking": 2})
            self.assertEqual(body["unknown_types"], {"mystery": 1})


# ── #8: one chat at a time, one import at a time ───────────────────────────────


class SavepointTests(unittest.TestCase):
    def test_a_failing_chat_is_rolled_back_and_the_rest_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            errorlog.configure(log_dir=root / "logs", db_path=root / "history.db")
            try:
                _, db_path = build_history(root, [claude_chat("old", 2)])
                newer = claude_chat("old", 3)
                newer["updated_at"] = "2025-01-01T00:00:00Z"
                records = [build_db.import_claude_conversation(c, i)
                           for i, c in enumerate([newer, claude_chat("fine", 1)])]
                conn = open_db(db_path)
                real = build_db._insert_conv_rows

                def fail_on_old(c, meta, msgs, *rest):
                    if meta["id"] == "old":
                        c.execute("INSERT INTO messages (conversation_id, content) VALUES ('old', 'half')")
                        raise RuntimeError("disk trouble")
                    return real(c, meta, msgs, *rest)

                with mock.patch.object(build_db, "_insert_conv_rows", fail_on_old):
                    counts = build_db.reconcile_backup(conn, records, "claude")
                conn.commit()
                self.assertEqual((counts["errors"], counts["added"]), (1, 1))
                # The failed chat is exactly as it was: no half-written messages.
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = 'old'").fetchone()[0], 4)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM search_index WHERE conversation_id = 'old'").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = 'fine'").fetchone()[0], 2)
                conn.close()
                log = json.loads((root / "logs" / "errors.json").read_text(encoding="utf-8"))
                self.assertIn("disk trouble", json.dumps(log))
            finally:
                errorlog.configure(log_dir=server.app_paths.LOG_DIR, db_path=server.app_paths.DB_PATH)

    def test_only_one_import_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "history.db"
            with import_lock.hold(db_path):
                with self.assertRaises(import_lock.ImportBusy):
                    with import_lock.hold(db_path):
                        pass
            with import_lock.hold(db_path):
                pass  # free again

    def test_a_second_import_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, db_path = build_history(root, [conversation("a", "One", ["gpt-4o"])])
            with import_lock.hold(db_path):
                status, body = call_server(root, db_path, "POST", "/api/import-new", {})
            self.assertEqual(status, 409)
            self.assertIn("already running", body["error"])
            # No backup was made for the refused request.
            self.assertFalse((root / "backups").exists() and any(
                "before-import" in p.name for p in (root / "backups").iterdir()))
            status, _ = call_server(root, db_path, "POST", "/api/import-new", {})
            self.assertEqual(status, 200)


class SchemaChangeTests(unittest.TestCase):
    def test_change_6_adds_the_count_columns_to_an_older_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, db_path = build_history(root, [conversation("a", "One", ["gpt-4o"])])
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE old_backups AS SELECT id, file_name, provider, file_hash, first_chat,
                    last_chat, total_chats, imported_at, added, updated, unchanged, skipped, errors
                    FROM imported_backups;
                DROP TABLE imported_backups;
                ALTER TABLE old_backups RENAME TO imported_backups;
                UPDATE schema_version SET version = 5;
            """)
            conn.commit()
            conn.close()
            server._ensure_runtime_schema(db_path)
            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(imported_backups)")}
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            conn.close()
            self.assertTrue({"dropped_types", "unknown_types"} <= cols)
            self.assertEqual(version, 6)
            self.assertTrue(any("before-db-change" in p.name for p in (root / "backups").iterdir()))

    def test_rebuild_waits_for_no_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, db_path = build_history(root, [conversation("a", "One", ["gpt-4o"])])
            with import_lock.hold(db_path):
                with self.assertRaises(import_lock.ImportBusy):
                    server.rebuild_history(db_path, root)


# ── #31: one chat at a time from disk, free space first ────────────────────────


class StreamingTests(unittest.TestCase):
    def test_date_range_counts_only_the_kept_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            short = claude_chat("dup", 1)
            short["created_at"] = short["updated_at"] = "2001-01-01T00:00:00Z"
            for m in short["chat_messages"]:
                m["created_at"] = "2001-01-01T00:00:00Z"
            path = write_export(Path(tmp) / "x.json", [short, claude_chat("dup", 3)])
            scan = build_db.scan_export(path, "claude")
            self.assertEqual(scan["winners"], {"dup": 1})
            self.assertGreater(scan["first"], build_db._iso_to_ts("2020-01-01T00:00:00Z"))

    def test_reads_every_item_across_small_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            items = [{"a": i, "s": "é" * i, "n": [1.5, None, True]} for i in range(50)] + [12345, "end"]
            path.write_text(" \n" + json.dumps(items, ensure_ascii=False) + "\n", encoding="utf-8")
            for size in (1, 3, 7, 64, 1 << 20):
                self.assertEqual(list(build_db.iter_export(path, chunk_size=size)), items)

    def test_broken_or_non_list_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            for text in ('[{"a": 1}, {"b": ', '[{"a": 1} {"b": 2}]', '[1, 2] extra', "[1,"):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError, msg=text):
                    list(build_db.iter_export(path, chunk_size=4))
            path.write_text('{"a": 1}', encoding="utf-8")
            with self.assertRaises(build_db.NotAList):
                list(build_db.iter_export(path))
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(list(build_db.iter_export(path)), [])

    def test_memory_stays_small_for_a_large_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.json"
            chat = claude_chat("x", 20)
            with open(path, "w", encoding="utf-8") as f:
                f.write("[")
                for i in range(4000):
                    chat["uuid"] = f"chat-{i}"
                    f.write(("," if i else "") + json.dumps(chat))
                f.write("]")
            size = path.stat().st_size
            self.assertGreater(size, 30_000_000)
            tracemalloc.start()
            try:
                count = sum(1 for _ in build_db.iter_export(path))
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(count, 4000)
            # A read buffer and one chat, not the whole file.
            self.assertLess(peak, 10_000_000)

    def test_build_refuses_without_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = write_export(root / "conversations.json", [claude_chat("c", 1)])
            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10)):
                with self.assertRaises(build_db.NotEnoughSpace) as cm:
                    build_db.build(src, root / "history.db")
            self.assertIn("free disk space", str(cm.exception))
            self.assertFalse((root / "history.db").exists())

    def test_import_refuses_without_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, db_path = build_history(root, [conversation("a", "One", ["gpt-4o"])])
            write_export(root / "new.json", [conversation("b", "Two", ["gpt-4o"])])
            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10)):
                status, body = call_server(root, db_path, "POST", "/api/import-new", {})
            self.assertEqual(status, 507)
            self.assertIn("free disk space", body["error"])
            conn = open_db(db_path)
            self.assertIsNone(conn.execute("SELECT 1 FROM conversations WHERE id = 'b'").fetchone())
            conn.close()


# ── #24: Upload asks before replacing ──────────────────────────────────────────


def call_server(root, db_path, method, path, body=None, raw=None, headers=None):
    errorlog.configure(log_dir=root / "logs", db_path=db_path)
    handler = type("TestHandler", (Handler,), {"db_path": db_path, "source_dir": root})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        h = {"Host": f"127.0.0.1:{port}"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"null")
        conn.close()
        if resp.status == 202 and isinstance(payload, dict) and payload.get("job"):
            deadline = time.monotonic() + 20
            job = payload["job"]
            while job["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
                job = background_jobs.get(db_path, job["id"])
            if job["status"] == "running":
                raise AssertionError("Test background job did not finish")
            if job["status"] == "failed":
                return job["error_status"], {"error": job["error"]}
            return 200, job["result"]
        return resp.status, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        errorlog.configure(log_dir=server.app_paths.LOG_DIR, db_path=server.app_paths.DB_PATH)


def multipart(fields):
    boundary = uuid.uuid4().hex
    out = b""
    for name, value, filename in fields:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += (f"--{boundary}\r\nContent-Disposition: {disp}\r\n\r\n").encode() + value + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


class UploadTests(unittest.TestCase):
    def upload(self, root, db_path, data, replace=False):
        fields = [("file", data, "image.png"), ("name", b"image.png", None)]
        if replace:
            fields.append(("replace", b"1", None))
        raw, headers = multipart(fields)
        return call_server(root, db_path, "POST", "/api/upload-file", raw=raw, headers=headers)

    def test_upload_asks_before_replacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, db_path = build_history(root, [conversation("a", "One", ["gpt-4o"])])
            status, body = self.upload(root, db_path, b"first")
            self.assertEqual((status, body["saved"]), (200, "image.png"))
            target = root / "files" / "image.png"
            self.assertEqual(target.read_bytes(), b"first")

            status, body = self.upload(root, db_path, b"second")
            self.assertEqual(status, 409)
            self.assertEqual(list(body), ["error"])
            self.assertIn("already", body["error"])
            self.assertEqual(target.read_bytes(), b"first")

            status, _ = self.upload(root, db_path, b"second", replace=True)
            self.assertEqual(status, 200)
            self.assertEqual(target.read_bytes(), b"second")

    def test_browser_asks_before_replacing(self):
        js = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        start = js.index('"/api/upload-file"')
        block = js[start - 1500:start + 2500]
        self.assertIn("expect: replace ? [] : [409]", block)
        self.assertIn("openConfirm(", block)
        self.assertIn('"replace"', block)


if __name__ == "__main__":
    unittest.main()
