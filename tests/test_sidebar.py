"""The optional Claude Desktop sidebar card: its build, its extension and its loader."""
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "sidebar" / "server" / "index.js"
HARNESS = ROOT / "tests" / "sidebar_harness.js"
NODE = shutil.which("node")


def built():
    temp = tempfile.TemporaryDirectory()
    subprocess.run([sys.executable, str(ROOT / "sidebar" / "make.py"), temp.name], check=True,
                   capture_output=True, timeout=30)
    return temp


class BuildTests(unittest.TestCase):
    def test_card_embeds_the_template_without_its_self_reload(self):
        with built() as out:
            card = (Path(out) / "sidebar.html").read_text(encoding="utf-8")
        literal = re.search(r"const PAGE = (\".*?\");\n", card, re.S).group(1)
        page = json.loads(literal)
        template = (ROOT / "template.html").read_text(encoding="utf-8")
        self.assertEqual(page, template.replace('<meta http-equiv="refresh" content="600">\n', ""))
        self.assertIn("/*__DATA__*/null", page)
        # The embedded template can't end the loader's own script early.
        self.assertNotIn("</script", literal)
        self.assertEqual(card.count("</script>"), 1)

    def test_extension_package_holds_exactly_the_manifest_icon_and_server(self):
        with built() as out, zipfile.ZipFile(Path(out) / "claude-usage-page.mcpb") as z:
            self.assertEqual(sorted(z.namelist()), ["icon.png", "manifest.json", "server/index.js"])
            self.assertEqual(z.read("server/index.js"), SERVER.read_bytes())
            manifest = json.loads(z.read("manifest.json"))
            icon = z.read(manifest["icon"])
        self.assertEqual(icon[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(icon[16:20], "big"), 512)  # width, from the PNG header
        self.assertEqual(manifest["server"]["entry_point"], "server/index.js")
        self.assertEqual([t["name"] for t in manifest["tools"]], ["get_usage"])
        # The card addresses the extension as host:<display name with spaces as underscores>.
        loader = (ROOT / "sidebar" / "loader.html").read_text(encoding="utf-8")
        self.assertIn(f'"host:{manifest["display_name"].replace(" ", "_")}"', loader)


@unittest.skipUnless(NODE, "needs Node to run the extension")
class ExtensionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / "port").write_text("1\n", encoding="ascii")  # nothing listens there

    def talk(self, *requests):
        lines = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests)
        run = subprocess.run([NODE, str(SERVER)], input=lines, capture_output=True, text=True, timeout=30,
                             env={**os.environ, "CLAUDE_USAGE_PAGE_DIR": str(self.home)})
        self.assertEqual(run.returncode, 0, run.stderr)
        return {r["id"]: r for r in map(json.loads, run.stdout.splitlines())}

    def call(self):
        return self.talk({"id": 1, "method": "tools/call", "params": {"name": "get_usage", "arguments": {}}})[1]["result"]

    def payload(self):
        result = self.call()
        self.assertEqual(len(result["content"]), 1)  # one copy only
        self.assertNotIn("structuredContent", result)
        return json.loads(result["content"][0]["text"])

    def test_lists_one_read_only_tool(self):
        replies = self.talk({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                            {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"})
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "claude-usage-page")
        tools = replies[2]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["get_usage"])
        self.assertEqual(tools[0]["annotations"], {"readOnlyHint": True, "openWorldHint": False})

    def test_hands_over_data_json_whole_even_past_the_pipe_buffer(self):
        data = {"generatedAt": "2026-11-20T12:00:00", "days": {f"d{i}": {"m": i, "pad": "x" * 200} for i in range(600)}}
        (self.home / "data.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.payload(), {"data": data, "refreshed": False})

    def test_missing_data_says_to_install_the_page(self):
        result = self.call()
        self.assertTrue(result["isError"])
        self.assertIn("Install Claude Usage Page first", result["content"][0]["text"])

    def test_a_snapshot_left_behind_by_an_older_page_is_not_served(self):
        (self.home / "data.json").write_text('{"generatedAt": "old", "days": {}}', encoding="utf-8")
        (self.home / "index.html").write_text("page", encoding="utf-8")
        self.assertEqual(self.payload()["data"]["generatedAt"], "old")  # built together: fine
        past = (self.home / "index.html").stat().st_mtime - 3600
        os.utime(self.home / "data.json", (past, past))  # the page moved on without it
        result = self.call()
        self.assertTrue(result["isError"])
        self.assertIn("Reinstall the current Claude Usage Page", result["content"][0]["text"])

    def test_unknown_tools_and_methods_are_errors(self):
        replies = self.talk({"id": 1, "method": "tools/call", "params": {"name": "rm", "arguments": {}}},
                            {"id": 2, "method": "resources/list"})
        self.assertEqual(replies[1]["error"]["code"], -32602)
        self.assertEqual(replies[2]["error"]["code"], -32601)

    def test_asks_the_page_server_to_rebuild_before_reading(self):
        hits = []

        class Page(http.server.BaseHTTPRequestHandler):
            status = 200

            def do_GET(self):
                hits.append(self.path)
                (Path(self.server.home) / "data.json").write_text('{"generatedAt": "fresh", "days": {}}', encoding="utf-8")
                self.send_response(self.status)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        server.home = self.home
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        (self.home / "port").write_text(str(server.server_address[1]), encoding="ascii")
        (self.home / "data.json").write_text('{"generatedAt": "stale", "days": {}}', encoding="utf-8")
        self.assertEqual(self.payload(), {"data": {"generatedAt": "fresh", "days": {}}, "refreshed": True})
        self.assertEqual(hits, ["/"])
        Page.status = 503
        self.assertFalse(self.payload()["refreshed"])


@unittest.skipUnless(NODE, "needs Node to run the card's script")
class LoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with built() as out:
            cls.card = (Path(out) / "sidebar.html").read_text(encoding="utf-8")

    def show(self, mcp):
        run = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"html": self.card, "mcp": mcp}),
                             capture_output=True, text=True, timeout=30, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_data_fills_the_template_and_watches_every_ten_minutes(self):
        data = {"generatedAt": "2026-11-20T12:00:00", "days": {}, "note": "</script><b>$&"}
        shown = self.show({"events": [{"data": {"data": data, "refreshed": True}}]})
        self.assertFalse(shown["page"]["hidden"])
        self.assertTrue(shown["note"]["hidden"])
        doc = shown["page"]["srcdoc"]
        self.assertIn('"generatedAt":"2026-11-20T12:00:00"', doc)
        self.assertNotIn("/*__DATA__*/null", doc)
        self.assertIn("\\u003c/script>\\u003cb>$&", doc)  # escaped like build.py, and "$&" kept as is
        self.assertEqual(shown["watches"], [{"server": "host:Claude_Usage_Page", "tool": "get_usage", "args": None,
                                             "options": {"cache": {"staleTime": 60000, "gcTime": 604800000},
                                                         "refetchInterval": 600000}}])

    def test_each_failure_says_what_to_do(self):
        cases = {
            "server_not_connected": "Claude Usage Page extension installed and turned on here",
            "approval_required": "needs your OK",
            "not_in_manifest": "Permissions menu",
            "server_unavailable": "didn't answer just now (server_unavailable)",
        }
        for code, words in cases.items():
            shown = self.show({"events": [{"error": {"code": code}}]})
            self.assertTrue(shown["page"]["hidden"], code)
            self.assertIn(words, shown["note"]["text"], code)
        other = self.show({"events": [{"error": {"code": "server_not_connected"}}]})["note"]["text"]
        self.assertIn("each one shows only its own usage", other)  # pins sync, data doesn't
        data = {"data": {"generatedAt": "a", "days": {}}}
        allowed = self.show({"events": [{"error": {"code": "approval_required"}}, {"click": "Allow and load"}, {"answer": data}]})
        self.assertFalse(allowed["page"]["hidden"])
        # Access ends while that call is still on its way: its answer must not bring the data back.
        for code in ("not_in_manifest", "server_not_connected"):  # access ended, or the extension went away
            late = self.show({"events": [{"error": {"code": "approval_required"}}, {"click": "Allow and load"},
                                         {"error": {"code": code}}, {"answer": data}]})
            self.assertTrue(late["page"]["hidden"], code)
            self.assertEqual(late["page"]["srcdoc"], "", code)
        org = self.show({"events": [{"error": {"code": "org_mcp_disabled"}}]})["note"]["text"]
        self.assertIn("administrator", org)
        tool = self.show({"events": [{"error": {"code": "tool_error", "message": "No usage data yet."}}]})
        self.assertEqual(tool["note"]["text"], "No usage data yet.")

    def test_a_passing_hiccup_keeps_the_last_data(self):
        data = {"generatedAt": "a", "days": {}}
        shown = self.show({"events": [{"data": {"data": data}}, {"error": {"code": "server_unavailable"}}]})
        self.assertFalse(shown["page"]["hidden"])
        self.assertEqual(shown["page"]["loads"], 1)  # left as it was, not reloaded with the same data
        again = self.show({"events": [{"data": {"data": data}}, {"data": {"data": data}}]})
        self.assertEqual(again["page"]["loads"], 1)  # a refetch with the same snapshot doesn't reload either
        newer = self.show({"events": [{"data": {"data": data}}, {"data": {"data": {**data, "generatedAt": "b"}}}]})
        self.assertEqual(newer["page"]["loads"], 2)
        # but losing access takes it down
        for code in ("not_in_manifest", "needs_reauth", "blocked_by_policy", "org_mcp_disabled", "consent_denied",
                     "approval_required", "user_changed"):
            gone = self.show({"events": [{"data": {"data": data}}, {"error": {"code": code}}]})
            self.assertTrue(gone["page"]["hidden"], code)
            self.assertEqual(gone["page"]["srcdoc"], "", code)  # unloaded, not just hidden
        switched = self.show({"events": [{"error": {"code": "user_changed"}}, {"data": {"data": data}}]})
        self.assertEqual(switched["stopped"], 1)
        self.assertTrue(switched["page"]["hidden"])  # nothing arrives once the watch is stopped
        self.assertIn("this computer's usage", switched["note"]["text"])

    def test_outside_the_claude_app_it_points_to_setup(self):
        shown = self.show("none")
        self.assertIn("pinned artifact in the Claude desktop app", shown["note"]["text"])
        self.assertTrue(shown["page"]["hidden"])
        self.assertEqual(shown["watches"], [])
