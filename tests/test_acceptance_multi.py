"""Multi-target acceptance runner — api_json + command dispatch paths.
Tests run against a real local HTTP server and real subprocesses — no Docker required.
"""
import json
import threading
import http.server
import socketserver
from pathlib import Path

from devagent.acceptance_runner import check_api_json, check_command


class _JSONHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"items": [{"id": 7}]}).encode())
    def log_message(self, *a): pass


def _serve():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _JSONHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_api_json_presence_and_equals():
    httpd, base = _serve()
    try:
        assert check_api_json(base, "/x", "GET", None, "items.0.id", None)["ok"]      # presence
        assert check_api_json(base, "/x", "GET", None, "items.0.id", 7)["ok"]          # equals
        assert not check_api_json(base, "/x", "GET", None, "items.0.id", 8)["ok"]      # mismatch
        assert not check_api_json(base, "/x", "GET", None, "missing.path", None)["ok"] # absent
    finally:
        httpd.shutdown()


def test_command_exit_and_stdout(tmp_path):
    r = check_command(str(tmp_path), ["python3", "-c", "print('hello-cli')"], 0, r"hello-")
    assert r["ok"]
    assert not check_command(str(tmp_path), ["python3", "-c", "import sys; sys.exit(3)"], 0, None)["ok"]
