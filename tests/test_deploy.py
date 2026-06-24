"""M3 local preview — start_preview's docker argv is mocked (no container); the DeployPhase
gets an injected fake start; DeployGate is exercised against a REAL local http.server."""

import functools
import http.server
import socketserver
import threading

from devagent import deploy
from devagent.deploy import DeployGate, DeployResult, start_preview
from devagent.phases.base import PhaseResult
from devagent.phases.deploy import DeployPhase


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def _serve(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def test_start_preview_argv_mounts_image_and_no_secret(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    res = start_preview(str(out), port=5599)
    argv = captured["argv"]
    assert "-p" in argv and "5599:8000" in argv
    assert "--user" in argv and "1000:1000" in argv
    assert any(a.endswith(":/out:ro") for a in argv)
    assert any(a.endswith("/preview.py:ro") for a in argv)
    assert deploy.DEFAULT_IMAGE in argv
    # no secret/key value anywhere in the recorded argv
    assert "sk-secret-value" not in " ".join(argv)
    assert "ANTHROPIC_API_KEY" not in argv
    assert res.url == "http://localhost:5599"
    assert res.container == "devagent-preview"


def test_start_preview_returns_error_on_docker_failure(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()

    calls = {"n": 0}

    def fake_run(argv, **kw):
        # first call is the `docker rm -f` cleanup (ignored); the run is the second
        calls["n"] += 1
        if calls["n"] == 1:
            return _Proc()
        p = _Proc()
        p.returncode = 1
        p.stderr = "docker: no such image"
        return p

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    res = start_preview(str(out), port=5599)
    assert res.url == ""
    assert "no such image" in (res.error or "")


def test_deploy_phase_passes_through_canned_result():
    canned = DeployResult(url="http://localhost:1234", container="devagent-preview")
    phase = DeployPhase(workdir="/whatever", start=lambda wd: canned)
    res = phase.run(None)
    assert res.exit_code == 0
    assert res.output_artifact is canned
    assert res.meta["url"] == "http://localhost:1234"


def test_deploy_phase_nonzero_on_empty_url():
    canned = DeployResult(url="", error="boom")
    phase = DeployPhase(workdir="/whatever", start=lambda wd: canned)
    res = phase.run(None)
    assert res.exit_code == 1
    assert res.output == "boom"


def test_deploy_gate_passes_for_a_live_url(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")
    httpd, url = _serve(tmp_path)
    try:
        art = DeployResult(url=url, container="devagent-preview")
        res = PhaseResult("deploy", 0, output_artifact=art)
        assert DeployGate().check(res).ok is True
    finally:
        httpd.shutdown()


def test_deploy_gate_fails_for_a_dead_url():
    art = DeployResult(url="http://127.0.0.1:1", container="devagent-preview")
    res = PhaseResult("deploy", 0, output_artifact=art)
    assert DeployGate().check(res).ok is False


def test_deploy_gate_fails_when_artifact_missing():
    res = PhaseResult("deploy", 1, output="deploy failed")
    assert DeployGate().check(res).ok is False


def test_deploy_gate_fails_on_empty_url():
    art = DeployResult(url="", error="boom")
    res = PhaseResult("deploy", 0, output_artifact=art)
    assert DeployGate().check(res).ok is False
