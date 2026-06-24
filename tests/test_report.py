"""M3 run report — a self-contained HTML summary of a run's ledger.

The renderer takes the ledger's events (and optional preview_url / acceptance) and
produces an offline HTML doc — no external/CDN assets — so a built run is inspectable
from a single file. Acceptance is passed in (not read from disk) to keep the renderer pure.
"""

from devagent.report import render_report, write_report


def _events():
    """A synthetic ledger covering the shapes the renderer must handle."""
    return [
        {"event": "input", "path": "examples/hello.md"},
        {"event": "egress", "network": "devagent-egress", "proxy": "http://devagent-proxy:3128"},
        {"event": "run_start", "phases": ["intake", "build"]},
        {"event": "phase", "phase": "intake", "exit": 0, "output": "Hello DevAgent",
         "meta": {"tokens_in": 900, "tokens_out": 150}},
        {"event": "gate", "phase": "intake", "gate": "brief_valid", "ok": True, "reason": ""},
        {"event": "phase", "phase": "build", "exit": 0, "output": "built /tmp/out",
         "meta": {"tokens_in": 62000, "tokens_out": 2300, "cost_usd": 0.1498,
                  "wall_clock_s": 34.5, "repairs": 1, "build_ok": True, "checks_pass": True}},
        {"event": "gate", "phase": "build", "gate": "rebuilds_and_passes_acceptance",
         "ok": True, "reason": ""},
        {"event": "run_end", "status": "succeeded", "detail": ""},
    ]


def test_report_contains_run_id_phases_and_pass():
    html = render_report(_events(), "run-abc-123")
    assert "run-abc-123" in html
    assert "intake" in html
    assert "build" in html
    assert "PASS" in html


def test_report_shows_cost_and_summed_tokens():
    html = render_report(_events(), "run-abc-123")
    assert "0.1498" in html  # build cost
    # summed tokens_in across phases: 900 + 62000 = 62900 (rendered with a thousands sep)
    assert "62,900" in html
    # summed tokens_out across phases: 150 + 2300 = 2450
    assert "2,450" in html
    assert "repairs" in html.lower()


def test_report_notes_egress_containment():
    html = render_report(_events(), "run-abc-123")
    assert "egress" in html.lower()
    assert "devagent-egress" in html


def test_report_includes_preview_url_when_given():
    url = "https://run-abc-123.preview.example.com"
    html = render_report(_events(), "run-abc-123", preview_url=url)
    assert url in html
    assert "Preview" in html


def test_report_renders_acceptance_checks_when_given():
    acceptance = [
        {"kind": "route_status", "route": "/", "ok": True, "detail": "status 200 (want 200)"},
        {"kind": "selector_present", "route": "/", "ok": False, "detail": "selector h1 not found"},
    ]
    html = render_report(_events(), "run-abc-123", acceptance=acceptance)
    assert "route_status" in html
    assert "selector_present" in html
    assert "status 200 (want 200)" in html
    assert "selector h1 not found" in html


def test_report_shows_fail_badge_on_failed_run():
    events = _events()
    # truncate to a failed gate + failed run_end
    events = events[:5] + [
        {"event": "gate", "phase": "intake", "gate": "brief_valid", "ok": False,
         "reason": "missing field"},
        {"event": "run_end", "status": "failed", "detail": "gate brief_valid failed: missing field"},
    ]
    html = render_report(events, "run-fail")
    assert "FAIL" in html
    assert "missing field" in html


def test_report_escapes_interpolated_text():
    events = _events()
    events[4] = {"event": "gate", "phase": "intake", "gate": "brief_valid", "ok": False,
                 "reason": "<script>alert(1)</script>"}
    html = render_report(events, "run-abc-123")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_is_self_contained_no_external_assets():
    html = render_report(_events(), "run-abc-123")
    assert "<style>" in html
    # no CDN / external resources — must render offline
    assert "http://" not in html.replace("http://devagent-proxy:3128", "")
    assert "src=" not in html
    assert "cdn" not in html.lower()


def test_write_report_creates_file(tmp_path):
    url = "https://preview.example.com/run-abc-123"
    acceptance = [{"kind": "route_status", "route": "/", "ok": True, "detail": "status 200"}]
    path = write_report(tmp_path, _events(), "run-abc-123",
                        preview_url=url, acceptance=acceptance)
    assert path == tmp_path / "report.html"
    assert path.exists()
    written = path.read_text(encoding="utf-8")
    assert "run-abc-123" in written
    assert url in written
    assert "status 200" in written
