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


def test_contains_value_is_exact_not_substring():
    from devagent.acceptance_runner import _contains_value
    assert _contains_value([{"id": "7", "title": "x"}], "7")        # nested in a list of dicts
    assert _contains_value({"data": [{"id": 42}]}, 42)              # numeric, deeper nesting
    # exact-value: "7" must NOT match a field that merely CONTAINS the digit 7
    # (the old `str(id) in json.dumps(payload)` substring check would wrongly pass here)
    assert not _contains_value([{"createdAt": "2027-01-07", "count": 70}], "7")
    assert not _contains_value([], "7")                            # empty store = missing


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


def _make_auth_server():
    """A tiny API where /todos requires `Authorization: Bearer good-token`; /login mints it."""
    store = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def _authed(self):
            return self.headers.get("Authorization") == "Bearer good-token"

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            if self.path == "/auth/register":
                return self._json(200, {"id": 1, "username": "testuser"})
            if self.path == "/auth/login":
                return self._json(200, {"token": "good-token"})
            if self.path == "/todos":
                if not self._authed():
                    return self._json(401, {"error": "unauthorized"})
                new_id = str(len(store) + 1)
                store[new_id] = {"title": "x"}
                return self._json(200, {"id": new_id})
            return self._json(404, {})

        def do_GET(self):
            if self.path == "/todos":
                if not self._authed():
                    return self._json(401, {"error": "unauthorized"})
                return self._json(200, [{"id": k, **v} for k, v in store.items()])
            return self._json(404, {})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_obtain_auth_header_logs_in_and_extracts_token():
    srv, base = _make_auth_server()
    try:
        headers, err = ar.obtain_auth_header(base, {
            "login_route": "/auth/login", "login_body": {"username": "testuser", "password": "pw"},
            "token_json_path": "token",
        })
        assert err is None
        assert headers == {"Authorization": "Bearer good-token"}
    finally:
        srv.shutdown()


def test_auth_check_passes_with_token_and_401s_without():
    srv, base = _make_auth_server()
    try:
        target = {
            "name": "api",
            "auth": {"login_route": "/auth/login", "register_route": "/auth/register",
                     "login_body": {"username": "testuser", "password": "pw"},
                     "token_json_path": "token"},
            "acceptance_checks": [
                # protected route WITH auth -> the runner sends the token -> 200/ok
                {"kind": "api_json", "route": "/todos", "method": "GET", "auth": True},
                {"kind": "persistence_survives_restart", "route": "/todos", "method": "POST",
                 "body": {"title": "x"}, "json_path": "id", "verify_route": "/todos", "auth": True},
                # unauth case: a protected route returns 401 with NO token
                {"kind": "route_status", "route": "/todos", "expected_status": 401},
            ],
        }
        results = ar.run_target_checks(target, base, workdir="/tmp", restart=lambda: base)
        by_kind = {(r["kind"], r.get("route")): r for r in results}
        assert by_kind[("api_json", "/todos")]["ok"] is True
        assert by_kind[("persistence_survives_restart", "/todos")]["ok"] is True
        assert by_kind[("route_status", "/todos")]["ok"] is True   # 401 == want 401, no token sent
    finally:
        srv.shutdown()


def test_auth_check_fails_cleanly_when_login_fails():
    srv, base = _make_auth_server()
    try:
        target = {
            "name": "api",
            "auth": {"login_route": "/nope", "token_json_path": "token"},  # bad login route -> 404
            "acceptance_checks": [{"kind": "api_json", "route": "/todos", "auth": True}],
        }
        results = ar.run_target_checks(target, base, workdir="/tmp")
        assert results[0]["ok"] is False and "auth required" in results[0]["detail"]
    finally:
        srv.shutdown()


def test_run_checks_dispatches_by_kind_and_flags_unknown(served):
    checks = [
        {"kind": "route_status", "route": "/", "expected_status": 200},
        {"kind": "frobnicate", "route": "/"},  # not a real kind
    ]
    results = ar.run_checks(checks, served)
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False and "unsupported" in results[1]["detail"]
