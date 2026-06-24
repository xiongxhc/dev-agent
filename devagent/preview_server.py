"""Serves the built app (/out/dist) with SPA fallback, for the local preview. Runs in the
M2 image (python3 only). Client-routed paths fall back to index.html so deep links work."""

import functools
import http.server
import socketserver
from pathlib import Path

DIST = Path("/out/dist")
PORT = 8000


class _Handler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        if not Path(self.translate_path(self.path)).is_file():
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    handler = functools.partial(_Handler, directory=str(DIST))
    socketserver.TCPServer(("0.0.0.0", PORT), handler).serve_forever()
