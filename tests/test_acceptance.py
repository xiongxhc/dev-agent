"""Acceptance runner — the kind-dispatch + HTTP (route_status) path, against a REAL local
static server. No browser, no Docker (selector_present/Playwright is docker-gated)."""

import pytest

from devagent import acceptance_runner as ar


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
