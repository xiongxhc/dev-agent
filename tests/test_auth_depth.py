"""M10 — auth & access depth: cookie/session auth, the actor permission matrix, and the
federated-IdP service seam. Schema validation + the deterministic runner paths against real
local servers (no browser, no Docker)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from devagent import acceptance_runner as ar
from devagent.schema import AcceptanceCheck, ArtifactSpec, AuthFlow


# --- schema ----------------------------------------------------------------

def test_cookie_mode_needs_no_token_path():
    f = AuthFlow(login_route="/login", mode="cookie", login_body={"u": "a"})
    assert f.mode == "cookie" and f.token_json_path == ""


def test_bearer_mode_still_requires_token_path():
    with pytest.raises(ValueError, match="token_json_path"):
        AuthFlow(login_route="/login", mode="bearer")


def test_unknown_auth_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        AuthFlow(login_route="/login", mode="magic", token_json_path="t")


def test_actor_alias_as_accepted_and_dumped_as_as_actor():
    c = AcceptanceCheck.model_validate({"kind": "route_status", "route": "/admin", "as": "admin",
                                        "expected_status": 200})
    assert c.as_actor == "admin"
    assert c.model_dump()["as_actor"] == "admin"


def test_check_referencing_undeclared_actor_rejected():
    with pytest.raises(ValueError, match="no actor with that name"):
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/admin",
                                                        as_actor="ghost")])


def test_actors_must_have_unique_names():
    a = AuthFlow(login_route="/login", token_json_path="t", name="dup")
    b = AuthFlow(login_route="/login", token_json_path="t", name="dup")
    with pytest.raises(ValueError, match="unique"):
        ArtifactSpec(type="backend", stack="node-express", name="api", actors=[a, b])


def test_actor_matrix_validates_when_actor_declared():
    admin = AuthFlow(login_route="/login", token_json_path="token", name="admin", role="admin",
                     login_body={"u": "admin"})
    member = AuthFlow(login_route="/login", token_json_path="token", name="member", role="member",
                      login_body={"u": "member"})
    spec = ArtifactSpec(type="backend", stack="node-express", name="api", actors=[admin, member],
                        acceptance_checks=[
                            AcceptanceCheck(kind="route_status", route="/admin", as_actor="admin",
                                            expected_status=200),
                            AcceptanceCheck(kind="route_status", route="/admin", as_actor="member",
                                            expected_status=403)])
    assert len(spec.actors) == 2


# --- runner: cookie/session auth -------------------------------------------

def _make_cookie_server():
    """/login sets `session=good` via Set-Cookie; /me needs that cookie back, else 401."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj, cookie=None):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            if self.path == "/login":
                return self._json(200, {"ok": True}, cookie="session=good; HttpOnly; Path=/")
            return self._json(404, {})

        def do_GET(self):
            if self.path == "/me":
                if self.headers.get("Cookie") == "session=good":
                    return self._json(200, {"user": "a"})
                return self._json(401, {})
            return self._json(404, {})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_cookie_auth_captures_and_replays_session():
    srv, base = _make_cookie_server()
    try:
        headers, err = ar.obtain_auth_header(base, {"login_route": "/login", "mode": "cookie",
                                                    "login_body": {"u": "a"}})
        assert err is None
        assert headers == {"Cookie": "session=good"}
    finally:
        srv.shutdown()


def test_cookie_auth_captures_session_set_on_a_redirect():
    """A 302 login that sets the cookie then redirects must not lose the cookie (the cookie jar
    captures it on the intermediate response, even though the final response carries none)."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            if self.path == "/login":
                self.send_response(302)
                self.send_header("Set-Cookie", "session=good; Path=/")
                self.send_header("Location", "/landing")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            # the redirect target carries NO Set-Cookie
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        headers, err = ar.obtain_auth_header(base, {"login_route": "/login", "mode": "cookie",
                                                    "login_body": {"u": "a"}})
        assert err is None
        assert headers == {"Cookie": "session=good"}
    finally:
        srv.shutdown()


def test_cookie_auth_check_passes_with_session_and_401s_without():
    srv, base = _make_cookie_server()
    try:
        target = {
            "name": "api",
            "auth": {"login_route": "/login", "mode": "cookie", "login_body": {"u": "a"}},
            "acceptance_checks": [
                {"kind": "route_status", "route": "/me", "expected_status": 200, "auth": True},
                {"kind": "route_status", "route": "/me", "expected_status": 401},  # no cookie
            ],
        }
        results = ar.run_target_checks(target, base, workdir="/tmp")
        assert results[0]["ok"] is True   # cookie replayed → 200
        assert results[1]["ok"] is True   # no cookie → 401
    finally:
        srv.shutdown()


# --- runner: the actor permission matrix -----------------------------------

def _make_rbac_server():
    """/login mints a token per user; /admin returns 200 for an admin token, 403 otherwise."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/login":
                return self._json(200, {"token": f"tok-{body.get('u')}"})
            return self._json(404, {})

        def do_GET(self):
            if self.path == "/admin":
                tok = self.headers.get("Authorization", "")
                return self._json(200 if tok == "Bearer tok-admin" else 403, {})
            return self._json(404, {})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_actor_matrix_admin_200_member_403():
    srv, base = _make_rbac_server()
    try:
        target = {
            "name": "api",
            "actors": [
                {"name": "admin", "login_route": "/login", "login_body": {"u": "admin"},
                 "token_json_path": "token"},
                {"name": "member", "login_route": "/login", "login_body": {"u": "member"},
                 "token_json_path": "token"},
            ],
            "acceptance_checks": [
                {"kind": "route_status", "route": "/admin", "as_actor": "admin", "expected_status": 200},
                {"kind": "route_status", "route": "/admin", "as_actor": "member", "expected_status": 403},
            ],
        }
        results = ar.run_target_checks(target, base, workdir="/tmp")
        assert all(r["ok"] for r in results)   # admin→200, member→403 both as expected
    finally:
        srv.shutdown()


def test_check_for_actor_whose_login_fails_fails_cleanly():
    srv, base = _make_rbac_server()
    try:
        target = {
            "name": "api",
            "actors": [{"name": "admin", "login_route": "/nope", "login_body": {"u": "admin"},
                        "token_json_path": "token"}],
            "acceptance_checks": [
                {"kind": "route_status", "route": "/admin", "as_actor": "admin", "expected_status": 200}],
        }
        results = ar.run_target_checks(target, base, workdir="/tmp")
        assert results[0]["ok"] is False and "auth required" in results[0]["detail"]
    finally:
        srv.shutdown()
