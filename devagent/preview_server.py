"""Serves the built app (/out/dist) with SPA fallback, for the local preview. Runs in the
M2 image (python3 only). Client-routed paths fall back to index.html so deep links work.

Resilient by construction: threaded (a slow or hung client can't block other requests),
per-request errors are swallowed, and a supervisor loop restarts the server if serve_forever
ever raises — so the process keeps serving for the life of the preview container instead of
silently dying and leaving the container 'Up' but unresponsive."""

import functools
import http.server
import time
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

    def handle_one_request(self):
        # A dropped connection is normal for a preview — swallow it so one disconnecting
        # client can never take the server down.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True


class _Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # never let a per-request error propagate or spam the log


def main() -> None:
    handler = functools.partial(_Handler, directory=str(DIST))
    while True:
        try:
            _Server(("0.0.0.0", PORT), handler).serve_forever()
        except Exception:  # noqa: BLE001 — whatever happens, rebind and keep serving
            time.sleep(0.5)


if __name__ == "__main__":
    main()
