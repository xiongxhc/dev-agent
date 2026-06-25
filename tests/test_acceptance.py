"""Acceptance runner — the kind-dispatch + HTTP (route_status) path, against a REAL local
static server. No browser, no Docker (selector_present/Playwright is docker-gated)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from devagent import acceptance_runner as ar
from devagent.acceptance_runner import check_persistence_survives_restart


def _make_store_server(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            new_id = str(len(store) + 1)
            store[new_id] = body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": new_id}).encode())

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([{"id": k, **v} for k, v in store.items()]).encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_persistence_check_passes_when_state_survives_restart():
    store = {}                                   # the "datastore" — survives the app restart
    srv, base = _make_store_server(store)
    try:
        # restart() simulates app bounce: same store, same base_url (datastore untouched).
        result = check_persistence_survives_restart(
            base, "/api/tasks", "POST", {"title": "buy milk"}, "id", "/api/tasks",
            restart=lambda: base)
        assert result["ok"] is True
    finally:
        srv.shutdown()


def test_persistence_check_fails_when_state_is_lost_on_restart():
    store = {}
    srv, base = _make_store_server(store)

    def restart_wipes_state():
        store.clear()                            # in-memory store loses everything on restart
        return base

    try:
        result = check_persistence_survives_restart(
            base, "/api/tasks", "POST", {"title": "x"}, "id", "/api/tasks",
            restart=restart_wipes_state)
        assert result["ok"] is False
    finally:
        srv.shutdown()


def test_persistence_check_fails_when_restart_unhealthy():
    srv, base = _make_store_server({})
    try:
        result = check_persistence_survives_restart(
            base, "/api/tasks", "POST", {"title": "x"}, "id", "/api/tasks",
            restart=lambda: None)               # app never came back
        assert result["ok"] is False and "restart" in result["detail"].lower()
    finally:
        srv.shutdown()


@pytest.fixture
def served(tmp_path):
    (tmp_path / "index.html").write_text("<html><body><h1 id='hero'>hi</h1></body></html>")
    httpd, base_url = ar._serve(tmp_path)
    yield base_url
    httpd.shutdown()


def test_route_status_passes_for_served_route(served):
    r = ar.check_route_status(served, "/", 200)
    assert r["ok"] is True and r["kind"] == "route_status"


def test_route_status_spa_fallback_serves_unknown_route_200(served):
    # A client-routed path has no file on disk; the SPA fallback serves index.html (200).
    r = ar.check_route_status(served, "/dashboard", 200)
    assert r["ok"] is True


def test_route_status_fails_on_status_mismatch(served):
    r = ar.check_route_status(served, "/", 404)  # server returns 200, we asked for 404
    assert r["ok"] is False
    assert "200" in r["detail"]


def test_route_status_fails_when_server_down(served):
    bad = served.replace(served.rsplit(":", 1)[1], "1")  # port 1 — nothing listening
    r = ar.check_route_status(bad, "/", 200)
    assert r["ok"] is False


def test_run_checks_dispatches_by_kind_and_flags_unknown(served):
    checks = [
        {"kind": "route_status", "route": "/", "expected_status": 200},
        {"kind": "frobnicate", "route": "/"},  # not a real kind
    ]
    results = ar.run_checks(checks, served)
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False and "unsupported" in results[1]["detail"]
