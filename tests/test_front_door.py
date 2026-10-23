"""The server's front door (AUDIT.md Phase 2, server side): Host check (#3),
request checks and safe file serving (#1), JSON error replies (#9) and size
limits (#10). Runs a real server on a throwaway database."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import errorlog
import request_limits
import server
from server import Handler
from tests.test_advanced_search import conversation
from tests.test_scratchpads import build_history


class FrontDoorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, db_path = build_history(self.root, [conversation("one", "First chat", ["gpt-4o"])])
        errorlog.configure(log_dir=self.root / "logs", db_path=db_path)
        handler = type("TestHandler", (Handler,), {"db_path": db_path})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        errorlog.configure(log_dir=server.app_paths.LOG_DIR, db_path=server.app_paths.DB_PATH)
        self.temp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        if data is not None and "Content-Length" not in h:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        if data is not None:
            conn.send(data)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def json_call(self, *args, **kwargs):
        resp, payload = self.call(*args, **kwargs)
        return resp.status, json.loads(payload)

    # #3
    def test_foreign_host_is_refused(self):
        status, body = self.json_call("GET", "/api/conversations",
                                      headers={"Host": f"attacker.example:{self.port}"})
        self.assertEqual(status, 403)
        self.assertIn("error", body)
        for host in (self.host, f"localhost:{self.port}"):
            status, _ = self.json_call("GET", "/api/conversations", headers={"Host": host})
            self.assertEqual(status, 200)

    # #1
    def test_changes_need_json_and_own_origin(self):
        status, _ = self.json_call("POST", "/api/scratchpads", raw=b'{"title": "x"}',
                                   headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.json_call("POST", "/api/scratchpads", {"title": "x"},
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.json_call("POST", "/api/scratchpads", {"title": "x"},
                                   headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _ = self.json_call("POST", "/api/recycle-bin/purge", {"all": True},
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.json_call("POST", "/api/upload-file", raw=b"x",
                                   headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.json_call("POST", "/api/scratchpads", {"title": "Mine"},
                                   headers={"Origin": f"http://{self.host}",
                                            "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)

    def test_user_files_never_render_as_pages(self):
        folder = self.root / "files"
        folder.mkdir()
        (folder / "evil.html").write_text("<script>alert(1)</script>")
        (folder / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        saved = dict(server._SOURCE_INDEX)
        try:
            server._SOURCE_INDEX["evil.html"] = [folder / "evil.html"]
            server._SOURCE_INDEX["pic.png"] = [folder / "pic.png"]
            resp, _ = self.call("GET", "/source/evil.html")
            self.assertTrue(resp.getheader("Content-Disposition").startswith("attachment"))
            self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
            resp, _ = self.call("GET", "/source/pic.png")
            self.assertTrue(resp.getheader("Content-Disposition").startswith("inline"))
        finally:
            server._SOURCE_INDEX.clear()
            server._SOURCE_INDEX.update(saved)

    # #9
    def test_malformed_requests_get_json_400(self):
        status, body = self.json_call("POST", "/api/scratchpads", raw=b"{}",
                                      headers={"Content-Type": "application/json",
                                               "Content-Length": "abc"})
        self.assertEqual(status, 400)
        status, body = self.json_call("POST", "/api/scratchpads", [1])
        self.assertEqual(status, 400)
        status, body = self.json_call("POST", "/api/scratchpads", raw=b"{nope",
                                      headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, body = self.json_call("GET", "/api/conversations?limit=abc")
        self.assertEqual(status, 400)
        self.assertIn("limit", body["error"])

    def test_unexpected_error_gets_json_500_and_is_logged(self):
        original = Handler._api_tags_all
        Handler._api_tags_all = lambda self: 1 / 0
        try:
            status, body = self.json_call("GET", "/api/tags")
        finally:
            Handler._api_tags_all = original
        self.assertEqual(status, 500)
        self.assertNotIn("ZeroDivision", body["error"])
        group = errorlog.read_log()["groups"][0]
        self.assertEqual(group["error"], "ZeroDivisionError")
        self.assertEqual(group["recent"][0]["status"], 500)

    def test_bad_numbers_and_unknown_routes_get_json(self):
        status, body = self.json_call("POST", "/api/tabs", {"conversation_id": "one",
                                                            "sort_index": "abc"})
        self.assertEqual(status, 400)
        status, body = self.json_call("GET", "/api/no-such-thing")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        status, body = self.json_call("DELETE", "/api/no-such-thing")
        self.assertEqual(status, 404)

    def test_refused_large_body_still_gets_its_reply(self):
        # The body is never read by the route; without draining it, closing
        # the connection would reset it before the reply arrived.
        status, body = self.json_call("POST", "/api/scratchpads", raw=b"x" * (5 * 1024 * 1024),
                                      headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("error", body)

    def test_stalled_body_does_not_hold_the_server(self):
        import socket, time
        sock = socket.create_connection(("127.0.0.1", self.port))
        sock.sendall((f"POST /api/scratchpads HTTP/1.1\r\nHost: {self.host}\r\n"
                      "Content-Type: text/plain\r\nContent-Length: 1000000\r\n\r\nxx").encode())
        sock.settimeout(15)
        start = time.monotonic()
        reply = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            reply += chunk
        sock.close()
        self.assertIn(b"415", reply.split(b"\r\n", 1)[0])
        self.assertLess(time.monotonic() - start, 10)

    # #10
    def test_imported_long_titles_still_work(self):
        status, _ = self.json_call("POST", "/api/tabs", {"conversation_id": "one",
                                                         "title": "t" * 5000})
        self.assertEqual(status, 200)
        status, _ = self.json_call("PATCH", "/api/conversation/one", {"title": "t" * 1001})
        self.assertEqual(status, 400)
        status, _ = self.json_call("PATCH", "/api/conversation/one", {"title": "t" * 1000})
        self.assertEqual(status, 200)

    def test_size_limits(self):
        original = request_limits.MAX_BODY
        request_limits.MAX_BODY = 1000
        try:
            status, body = self.json_call("POST", "/api/scratchpads", {"content": "x" * 2000})
        finally:
            request_limits.MAX_BODY = original
        self.assertEqual(status, 413)
        status, body = self.json_call("POST", "/api/labels", {"name": "n" * 61, "color": "#123456"})
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])
        status, _ = self.json_call("POST", "/api/labels", {"name": "n" * 60, "color": "#123456"})
        self.assertEqual(status, 200)
        status, body = self.json_call("POST", "/api/tags", {"conv_id": "one", "tag": "t" * 41})
        self.assertEqual(status, 400)
        big = "p" * (request_limits.PREFERENCE_VALUE_MAX + 1)
        for payload in ({"preferences": {"x": big}}, {"x": big}):
            status, _ = self.json_call("PATCH", "/api/preferences", payload)
            self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
