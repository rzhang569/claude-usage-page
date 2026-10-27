"""Serve index.html (and nothing else) on 127.0.0.1, so the card opens as a
normal web page in any browser, including the Claude desktop app's built-in one.
Opening or reloading the page rebuilds it first when it is more than a minute
old, so a reload always shows the latest usage.
Usage: python3 serve.py [port]   (default 8788)"""
import http.server
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
BUILD = os.path.join(HERE, "build.py")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
# Seconds a built page counts as fresh; a negative value turns rebuilding off.
REBUILD_AFTER = float(os.environ.get("CLAUDE_USAGE_REBUILD_AFTER", "60"))

building = threading.Lock()


def refresh():
    """Rebuild the page if it is missing or stale. One build at a time; requests
    that arrive meanwhile wait for it and then reuse its result."""
    if REBUILD_AFTER < 0:
        return
    with building:
        try:
            age = time.time() - os.path.getmtime(PAGE)
        except OSError:
            age = None
        if age is not None and age <= REBUILD_AFTER:
            return
        # Keep the console hidden when run by pythonw on Windows.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run([sys.executable, BUILD], cwd=HERE, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            pass  # Serve the last good page if the build fails.


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        refresh()
        try:
            with open(PAGE, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(503, "Not built yet: run build.py")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
