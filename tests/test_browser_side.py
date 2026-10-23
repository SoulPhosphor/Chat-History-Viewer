"""Phase 2, browser side (AUDIT.md session 3): the shared server-call helper
(#23), the Error Log page and browser errors (#35), escaped math text (#2),
KaTeX kept inside the app (#15) and the Content-Security-Policy (#1).
Server parts run on a throwaway database and log folder."""
import json
import re
import subprocess
import unittest
from pathlib import Path

import errorlog
from tests import test_front_door

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "static"
APP_JS = (STATIC / "app.js").read_text()
INDEX = (STATIC / "index.html").read_text()


def run_node(script):
    result = subprocess.run(["node", "-e", script], cwd=ROOT, check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout)


def markdown(text):
    """Render `text` with the app's markdown renderer, outside a browser."""
    start = APP_JS.index("const md = (() => {")
    end = APP_JS.index("})();", start) + len("})();")
    return run_node(APP_JS[start:end] + f"\nprocess.stdout.write(JSON.stringify(md({json.dumps(text)})));")


class MathEscapeTests(unittest.TestCase):
    # #2
    def test_html_between_dollar_amounts_stays_text(self):
        html = markdown("I paid $5 for <img src=x onerror=alert(1)> and $10 later")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_math_text_is_kept_for_katex(self):
        html = markdown(r"Area $a < b$ and $$\begin{aligned} x &= 1 \end{aligned}$$")
        self.assertIn("$a &lt; b$", html)
        self.assertIn(r"$$\begin{aligned} x &amp;= 1 \end{aligned}$$", html)

    def test_eschtml_escapes_quotes(self):
        start = APP_JS.index("function escHtml(s) {")
        end = APP_JS.index("\n}\n", start) + 2
        out = run_node(APP_JS[start:end] + '\nprocess.stdout.write(JSON.stringify(escHtml(\'a"<b>&\')));')
        self.assertEqual(out, "a&quot;&lt;b&gt;&amp;")


class HelperTests(unittest.TestCase):
    # #23
    def test_every_server_call_goes_through_the_helper(self):
        for path in STATIC.glob("*.js"):
            if path.name == "api.js":
                continue
            self.assertNotIn("fetch(", path.read_text(), f"{path.name} calls fetch() directly")

    def test_helper_loads_first_and_is_valid(self):
        scripts = re.findall(r'<script src="([^"]+)"', INDEX)
        self.assertEqual(scripts[0], "/static/api.js")
        subprocess.run(["node", "--check", "static/api.js"], cwd=ROOT, check=True)

    def test_helper_reports_failure_and_throws(self):
        api = (STATIC / "api.js").read_text()
        script = """
const shown = [], sent = [];
globalThis.window = { addEventListener() {}, innerWidth: 800, innerHeight: 600 };
globalThis.document = {
  addEventListener() {},
  getElementById(id) {
    return id === "app-notice" ? { set hidden(v) {} } :
           id === "app-notice-text" ? { set textContent(v) { shown.push(v); } } : null;
  },
};
Object.defineProperty(globalThis, "navigator", { value: { userAgent: "Mozilla/5.0 Firefox/142.0" }, configurable: true });
globalThis.fetch = async (url, init) => {
  if (url === "/api/error-log/browser") { sent.push(JSON.parse(init.body)); return {}; }
  return { ok: false, status: 400, json: async () => ({ error: "Plain message." }) };
};
""" + api + """
(async () => {
  let caught;
  try { await apiRequest("/api/tags?q=secret", { method: "POST", body: { tag: "x" }, action: "Add tag" }); }
  catch (e) { caught = e; }
  process.stdout.write(JSON.stringify({ shown, sent, msg: caught.message, status: caught.status, reported: caught.reported }));
})();
"""
        out = run_node(script)
        self.assertEqual(out["shown"], ["Plain message."])
        self.assertEqual(out["msg"], "Plain message.")
        self.assertEqual(out["status"], 400)
        self.assertTrue(out["reported"])
        self.assertEqual(out["sent"][0]["action"], "Add tag")
        self.assertEqual(out["sent"][0]["browser"]["name"], "Firefox")

    def test_no_raw_python_errors_in_replies(self):
        source = (ROOT / "server.py").read_text()
        self.assertIsNone(re.search(r'"error": f"[^"]*\{e\}', source))


class ErrorLogTests(unittest.TestCase):
    # #35
    def test_browser_stack_frames(self):
        chrome = ("TypeError: x is undefined\n"
                  "    at renderThing (http://127.0.0.1:5174/static/app.js:120:7)\n"
                  "    at async openThing (http://127.0.0.1:5174/static/app.js:80:3)")
        frames = errorlog.browser_frames(chrome)
        self.assertEqual(frames[-1], {"file": "static/app.js", "line": 120,
                                      "function": "renderThing", "code": ""})
        firefox = "renderThing@http://127.0.0.1:5174/static/app.js?v=2:120:7\n"
        self.assertEqual(errorlog.browser_frames(firefox)[0]["file"], "static/app.js")


class ErrorLogServerTests(test_front_door.FrontDoorTests):
    """Reuses the front-door test server (a throwaway database and log)."""

    def test_browser_error_is_logged_without_private_text(self):
        status, _ = self.json_call("POST", "/api/error-log/browser", {
            "error_type": "RequestFailed",
            "message": "Failed: 'my secret search words here'",
            "stack": "at x (http://127.0.0.1/static/app.js:5:1)",
            "action": "Search",
            "request": {"method": "GET", "url": "/api/search?q=private+words&limit=40"},
            "status": 500,
            "browser": {"name": "Firefox", "version": "142", "window": "1440×900", "screen": "conversation"},
        })
        self.assertEqual(status, 200)
        text = (self.root / "logs" / "errors.json").read_text()
        self.assertNotIn("private", text)
        self.assertNotIn("secret", text)
        group = errorlog.read_log()["groups"][0]
        self.assertEqual(group["where"], "browser")
        self.assertEqual(group["action"], "Search (GET /api/search?limit=…&q=…)")
        self.assertEqual(group["location"], "static/app.js:5")
        self.assertEqual(group["recent"][0]["browser"]["name"], "Firefox")

    def test_other_websites_cannot_write_to_the_log(self):
        status, _ = self.json_call("POST", "/api/error-log/browser", {"message": "x"},
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(errorlog.read_log()["groups"], [])

    def test_page_shows_ten_newest_groups_and_clear_empties_the_file(self):
        for i in range(12):
            errorlog.record(where="browser", error_type=f"E{i}", message="m",
                            action=f"action {chr(97 + i)}")
        errorlog.record(where="browser", error_type="E0", message="m", action="action a")
        status, body = self.json_call("GET", "/api/error-log")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["groups"]), 10)
        self.assertEqual(body["groups"][0]["error"], "E0")
        self.assertEqual(body["groups"][0]["count"], 2)
        status, _ = self.json_call("DELETE", "/api/error-log")
        self.assertEqual(status, 200)
        self.assertEqual(errorlog.read_log()["groups"], [])

    # #15 and #1
    def test_app_page_has_security_policy_and_no_internet_code(self):
        resp, payload = self.call("GET", "/")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertNotRegex(payload.decode(), r'(src|href)="https?://')

    def test_katex_is_served_from_the_app(self):
        for path, ctype in (("/static/vendor/katex/katex.min.js", "text/javascript"),
                            ("/static/vendor/katex/contrib/auto-render.min.js", "text/javascript"),
                            ("/static/vendor/katex/katex.min.css", "text/css"),
                            ("/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2", "font/woff2")):
            resp, _ = self.call("GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype), path)
        for bad in ("/static/vendor/katex/../../server.py", "/static/vendor/katex/LICENSE",
                    "/static/vendor/katex/%2e%2e/%2e%2e/server.py"):
            resp, _ = self.call("GET", bad)
            self.assertEqual(resp.status, 404, bad)
        css = (STATIC / "vendor/katex/katex.min.css").read_text()
        for font in set(re.findall(r"url\((fonts/[^)]+\.woff2)\)", css)):
            self.assertTrue((STATIC / "vendor/katex" / font).is_file(), font)


# The front-door tests themselves already run from test_front_door.py.
for _name in [n for n in dir(test_front_door.FrontDoorTests) if n.startswith("test_")]:
    setattr(ErrorLogServerTests, _name, None)


if __name__ == "__main__":
    unittest.main()
