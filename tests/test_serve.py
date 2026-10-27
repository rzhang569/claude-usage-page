"""Exercise the real HTTP server in a subprocess, serving only temporary files."""
import concurrent.futures
import contextlib
import http.client
import socket
import subprocess
import sys
import tempfile
import time
import os
import unittest

from tests import fixtures
from tests.fixtures import Fixture


class ServerTests(unittest.TestCase):
    # These tests serve a hand-written page, so rebuilding on load stays off.
    rebuild_after = "-1"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-usage-server-test-")
        self.addCleanup(self.temp.cleanup)
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        self.fixture = Fixture(self.temp.name)
        self.page = self.fixture.app / "index.html"
        self.body = (f"<!doctype html><html><body>Fake usage — 测试 {self.fixture.root.name}</body></html>"
                     .encode("utf-8"))
        self.page.write_bytes(self.body)
        # These existing files must never be exposed by the server.
        (self.fixture.app / "cache.json").write_text('{"private": true}', encoding="utf-8")
        (self.fixture.app / "secret.txt").write_text("private", encoding="utf-8")
        self.start_server()

    def stop_server(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def start_server(self):
        # Release a kernel-assigned free port just before launching. Retry if another
        # process wins that brief race; readiness is checked via HTTP, never a fixed sleep.
        for attempt in range(5):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                self.port = listener.getsockname()[1]
            log = self.resources.enter_context(tempfile.TemporaryFile(mode="w+b"))
            process = subprocess.Popen([sys.executable, str(self.fixture.app / "serve.py"), str(self.port)],
                                       cwd=self.fixture.root,
                                       env={**self.fixture.env(), "CLAUDE_USAGE_REBUILD_AFTER": self.rebuild_after},
                                       stdout=subprocess.DEVNULL, stderr=log)
            self.addCleanup(self.stop_server, process)
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    response = self.request("/")
                except (OSError, http.client.HTTPException):
                    time.sleep(0.02)
                    continue
                if response[0] == 200 and response[2] == self.body:
                    self.process = process
                    return
                time.sleep(0.02)
            self.stop_server(process)
            log.seek(0)
            error = log.read().decode("utf-8", errors="replace")
            if "Address already in use" not in error and "Only one usage" not in error:
                self.fail(f"Server did not become ready: {error}")
        self.fail("Could not start server after five port allocation attempts")

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_root_and_index_return_exact_html_with_no_store_and_byte_length(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(int(headers["Content-Length"]), len(self.body))

    def test_query_strings_on_allowed_routes_return_page(self):
        for path in ("/?refresh=1", "/index.html?refresh=1&name=test", "/?path=/cache.json"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.body)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_every_other_path_returns_404_including_existing_private_files(self):
        for path in ("/favicon.ico", "/secret.txt", "/cache.json", "/serve.py", "/template.html",
                     "/projects/test.jsonl", "/index.html/", "/INDEX.HTML", "//index.html/extra",
                     "/../secret.txt", "/%2e%2e/secret.txt", "/%69ndex.html", "/missing?x=1"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'"private": true', body)

    def test_missing_index_returns_503_and_recovers_without_restart(self):
        self.page.unlink()
        for path in ("/", "/index.html", "/?refresh=1"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 503)
                self.assertIn(b"Not built yet: run build.py", body)
        self.assertEqual(self.request("/cache.json")[0], 404)
        self.page.write_bytes(self.body)
        self.assertEqual(self.request("/")[0], 200)

    def test_subsequent_requests_read_replaced_page(self):
        replacement = self.fixture.app / "replacement.tmp"
        replacement.write_bytes(b"new page")
        replacement.replace(self.page)
        status, headers, body = self.request("/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"new page")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), 8)

    def test_build_subprocess_output_is_served_without_server_restart(self):
        result = subprocess.run([sys.executable, str(self.fixture.app / "build.py")],
                                cwd=self.fixture.root, env=self.fixture.env(),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        built = self.page.read_bytes()
        self.assertIn(b"const DATA = {", built)
        self.assertNotIn(b"/*__DATA__*/null", built)
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, built)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_parallel_requests_are_served_successfully(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(self.request, ["/", "/index.html"] * 8))
        for status, headers, body in responses:
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(body, self.body)
        self.assertIsNone(self.process.poll())


class RebuildOnLoadTests(ServerTests):
    """A request rebuilds a stale page from the fake Claude data before serving it."""
    rebuild_after = "60"

    def make_stale(self):
        old = time.time() - 3600
        os.utime(self.page, (old, old))

    def test_stale_page_is_rebuilt_before_it_is_served(self):
        self.make_stale()
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"const DATA = {", body)
        self.assertNotEqual(body, self.body)
        self.assertEqual(body, self.page.read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_fresh_page_is_served_without_rebuilding(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[2], self.body)

    def test_new_transcript_shows_up_on_the_next_load(self):
        self.make_stale()
        before = self.request("/")[2]
        self.fixture.write_transcript("delta/later.jsonl", [
            fixtures.row("later-user", "2026-01-04T12:00:00Z", "later"),
            fixtures.row("later-answer", "2026-01-04T12:05:00Z", "later", "assistant",
                         counts=(7, 7, 7, 7)),
        ])
        self.make_stale()
        after = self.request("/")[2]
        self.assertNotIn(b'"2026-01-04"', before)
        self.assertIn(b'"2026-01-04"', after)

    def test_missing_page_is_built_on_first_request(self):
        self.page.unlink()
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"const DATA = {", body)

    def test_failed_build_keeps_serving_the_last_page(self):
        (self.fixture.app / "template.html").unlink()
        self.make_stale()
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.body)

    def test_parallel_requests_on_a_stale_page_all_get_the_same_rebuilt_page(self):
        self.make_stale()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(self.request, ["/"] * 12))
        bodies = {body for status, _, body in responses if status == 200}
        self.assertEqual(len(responses), 12)
        self.assertEqual(len(bodies), 1)
        self.assertIn(b"const DATA = {", bodies.pop())

    # The inherited cases below assume the hand-written page is served untouched
    # or that a missing page stays missing; they don't apply once rebuilding is on.
    test_missing_index_returns_503_and_recovers_without_restart = None
    test_build_subprocess_output_is_served_without_server_restart = None
