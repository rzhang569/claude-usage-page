"""Exercise the real HTTP server in a subprocess, serving only temporary files."""
import concurrent.futures
import contextlib
import http.client
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.fixtures import Fixture


class ServerTests(unittest.TestCase):
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
                                       cwd=self.fixture.root, env=self.fixture.env(),
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
