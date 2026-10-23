"""One set of rules for every query (AUDIT.md #25 and #13): every conversation
list, search and report leaves out the Recycle Bin, keeps to the side the
sidebar shows (Claude or ChatGPT), and shows the user's rename. The first
tests record how the sidebar lists and Advanced Search behaved before the
rules moved into query_rules.py; the last ones bring quick search and the
Attachment Report into line. Throwaway database with made-up data only."""
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import advanced_search
import errorlog
import query_rules
import server
from server import Handler, open_db
from tests.test_advanced_search import conversation
from tests.test_scratchpads import build_history

# id → title in the export. "renamed" gets a new name, "binned" goes to the
# Recycle Bin, "archived" is archived and "other" is a Claude chat; the rest
# are ChatGPT chats.
CHATS = {
    "keep": "Garden plans",
    "renamed": "Old name",
    "binned": "Garden in the bin",
    "archived": "Garden archived",
    "other": "Garden on Claude",
}
RENAMED = "Garden renamed"


class QueryRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.db_path = build_history(
            self.root, [conversation(cid, title, ["gpt-4o"]) for cid, title in CHATS.items()])
        conn = open_db(self.db_path)
        conn.execute("UPDATE conversations SET provider = 'claude' WHERE id = 'other'")
        conn.executemany(
            "INSERT INTO conversation_meta(conversation_id, custom_title, archived, deleted) "
            "VALUES (?, ?, ?, ?)",
            [("renamed", RENAMED, 0, 0), ("binned", None, 0, 1), ("archived", None, 1, 0)],
        )
        for cid in CHATS:
            conn.execute(
                "UPDATE messages SET attachments = ? WHERE conversation_id = ? AND role = 'user'",
                (json.dumps([{"name": f"{cid}.pdf", "type": "pdf"}]), cid),
            )
            conn.execute(
                "INSERT INTO udb.conversation_bookmarks(id, conversation_id, seq, name) "
                "VALUES (?, ?, 0, 'Mark')", (f"bm-{cid}", cid))
            conn.execute(
                "INSERT INTO pinned_conversations(conversation_id, pinned_at, order_index) "
                "VALUES (?, 1, 0)", (cid,))
        conn.execute("INSERT INTO udb.folders(id, name, provider) VALUES ('f1', 'Box', 'chatgpt')")
        conn.execute("INSERT INTO udb.folder_items(conversation_id, folder_id) VALUES ('renamed', 'f1')")
        conn.execute("INSERT INTO udb.folder_items(conversation_id, folder_id) VALUES ('binned', 'f1')")
        conn.commit()
        conn.close()
        errorlog.configure(log_dir=self.root / "logs", db_path=self.db_path)
        handler = type("TestHandler", (Handler,), {"db_path": self.db_path})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        errorlog.configure(log_dir=server.app_paths.LOG_DIR, db_path=server.app_paths.DB_PATH)
        self.temp.cleanup()

    def get(self, path, **params):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        url = path + ("?" + urllib.parse.urlencode(params) if params else "")
        conn.request("GET", url, headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200, body)
        return body

    def sidebar(self, **params):
        rows = self.get("/api/conversations", provider="chatgpt", **params)["conversations"]
        return {r["id"]: r["title"] for r in rows}

    # ── Recorded before the change: these must stay the same ───────────────

    def test_sidebar_views(self):
        # "renamed" lives in a folder, so it is only in the folder list.
        self.assertEqual(self.sidebar(view="recent"), {})  # everything is pinned
        self.assertEqual(set(self.sidebar(view="all")), {"keep", "archived"})
        self.assertEqual(set(self.sidebar(view="archived")), {"archived"})
        self.assertEqual(set(self.sidebar(view="deleted")), {"binned"})
        self.assertEqual(set(self.sidebar(view="pinned")), {"keep", "archived"})

    def test_sidebar_search_uses_the_rename(self):
        self.assertEqual(self.sidebar(view="deleted", q="Garden"), {"binned": "Garden in the bin"})
        self.assertEqual(set(self.sidebar(view="all", q="Garden")), {"keep", "archived"})
        found = self.get("/api/conversations", provider="chatgpt", view="label:__unlabeled__", q="renamed")
        self.assertEqual({r["id"]: r["title"] for r in found["conversations"]}, {"renamed": RENAMED})
        # Short searches skip the full-text index and use the title only.
        found = self.get("/api/conversations", provider="chatgpt", view="label:__unlabeled__", q="re")
        self.assertIn("renamed", {r["id"] for r in found["conversations"]})
        # A search the full-text index can't read falls back to titles and
        # tags instead of failing.
        found = self.get("/api/conversations", provider="chatgpt", view="all", q='"Garden')
        self.assertEqual(found["conversations"], [])
        self.assertEqual(self.get("/api/search", q='"Garden', provider="chatgpt")["results"], [])

    def test_pinned_folders_and_bookmarks(self):
        pinned = self.get("/api/pinned", provider="chatgpt")
        rows = pinned if isinstance(pinned, list) else pinned.get("pinned", [])
        self.assertEqual({r["conversation_id"] for r in rows}, {"keep", "renamed", "archived"})
        folders = self.get("/api/folders", provider="chatgpt")["folders"]
        items = {c["id"]: c["title"] for f in folders for c in f["conversations"]}
        self.assertEqual(items, {"renamed": RENAMED})
        marks = self.get("/api/global-bookmarks", provider="chatgpt")["conversations"]
        self.assertEqual({c["id"]: c["title"] for c in marks},
                         {"keep": "Garden plans", "renamed": RENAMED, "archived": "Garden archived"})

    def test_advanced_search(self):
        conn = open_db(self.db_path)
        try:
            data = advanced_search.search(conn, {
                "query": "Garden", "providers": ["chatgpt"], "statuses": ["active"],
                "search_in": {"include": ["titles"], "exclude": []},
            })
        finally:
            conn.close()
        self.assertEqual({r["id"]: r["title"] for r in data["results"]},
                         {"keep": "Garden plans", "renamed": RENAMED})

    def test_media_hub_leaves_out_the_recycle_bin(self):
        sections = {s["key"]: s for s in self.get("/api/gallery")["sections"]}
        groups = {g["conv_id"]: g["conv_title"] for g in sections["attachments"]["groups"]}
        self.assertEqual(groups, {"keep": "Garden plans", "renamed": RENAMED,
                                  "archived": "Garden archived", "other": "Garden on Claude"})

    # ── Quick search and the Attachment Report follow the same rules (#13) ──

    def test_quick_search_follows_the_sidebar_rules(self):
        results = self.get("/api/search", q="Garden", provider="chatgpt")["results"]
        self.assertEqual({r["conv_id"]: r["conv_title"] for r in results},
                         {"keep": "Garden plans", "renamed": RENAMED, "archived": "Garden archived"})
        # Matches the rename, and shows it.
        results = self.get("/api/search", q="Garden renamed", provider="chatgpt")["results"]
        self.assertEqual({r["conv_id"]: r["conv_title"] for r in results}, {"renamed": RENAMED})
        # With no side given, both sides, still never the Recycle Bin.
        results = self.get("/api/search", q="Garden")["results"]
        self.assertEqual({r["conv_id"] for r in results}, {"keep", "renamed", "archived", "other"})
        # Message text matches obey the same rules.
        results = self.get("/api/search", q="Find this", provider="claude")["results"]
        self.assertEqual({r["conv_id"] for r in results}, {"other"})

    def test_quick_search_keeps_a_rename_match_among_many_text_matches(self):
        conn = open_db(self.db_path)
        for i in range(30):
            conn.execute("INSERT INTO conversations(id, title, provider) VALUES (?, 'Filler', 'chatgpt')",
                         (f"fill{i}",))
            conn.execute("INSERT INTO search_index(conversation_id, title, body) VALUES (?, 'Filler', 'Garden')",
                         (f"fill{i}",))
        conn.commit()
        conn.close()
        results = self.get("/api/search", q="Garden", provider="chatgpt")["results"]
        self.assertIn("renamed", {r["conv_id"] for r in results})

    def test_attachment_report_follows_the_sidebar_rules(self):
        report = self.get("/api/attachment-report", provider="chatgpt")
        self.assertEqual({m["conv_id"]: m["conv_title"] for m in report["missing"]},
                         {"keep": "Garden plans", "renamed": RENAMED, "archived": "Garden archived"})
        self.assertEqual(report["total"], 3)
        report = self.get("/api/attachment-report")
        self.assertEqual({m["conv_id"] for m in report["missing"]},
                         {"keep", "renamed", "archived", "other"})

    # ── The helpers themselves ──────────────────────────────────────────────

    def test_provider_is_checked_against_the_list(self):
        self.assertEqual(query_rules.provider_or_none(" ChatGPT "), "chatgpt")
        self.assertIsNone(query_rules.provider_or_none("x' OR 1=1 --"))
        self.assertEqual(query_rules.and_provider(None), "")
        self.assertEqual(query_rules.and_provider("claude"), " AND c.provider = 'claude'")
        with self.assertRaises(ValueError):
            query_rules.and_provider("x' OR 1=1 --")


if __name__ == "__main__":
    unittest.main()
