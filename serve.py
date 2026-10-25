"""Serve index.html (and nothing else) on 127.0.0.1, so the card opens as a
normal web page in any browser, including the Claude desktop app's built-in one.
Usage: python3 serve.py [port]   (default 8788)"""
import http.server
import os
import sys

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8788


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
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
