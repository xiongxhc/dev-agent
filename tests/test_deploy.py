"""M3 local preview — start_preview's docker argv is mocked (no container); the DeployPhase
gets an injected fake start; DeployGate is exercised against a REAL local http.server.
M6: per-target preview (backend detached + frontend static, wired via /config.json)."""

import functools
import http.server
import socketserver
import threading

from devagent import deploy
from devagent.deploy import DeployGate, DeployResult, start_preview
from devagent.phases.base import PhaseContext, PhaseResult
from devagent.phases.deploy import DeployPhase
from devagent.schema import ArtifactSpec, ProjectScope


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


# ---------------------------------------------------------------------------
# M6: per-target preview
# ---------------------------------------------------------------------------

def test_deploy_starts_each_target(monkeypatch):
    started = []

    def fake_start(workdir, target, network=None, env=None):   # per-target starter
        started.append(target.name)
        return f"http://127.0.0.1:90/{target.name}"

    scope = ProjectScope(title="A", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     acceptance_checks=[]),
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                     acceptance_checks=[]),
    ])
    ctx = PhaseContext(sandbox=None, budget=None, ledger=None, artifacts={"scope": scope})
    res = DeployPhase(workdir="/out", start_target=fake_start).run(ctx)
    # Backend must start before frontend — assert list, not set
    assert started == ["api", "web"]
    assert res.exit_code == 0
    assert "web" in res.output_artifact.urls and "api" in res.output_artifact.urls


def test_deploy_gate_fails_when_one_target_is_dead(tmp_path):
    """Gate must fail if ANY target is unreachable (even if the primary URL is live)."""
    # Spin up a real HTTP server for the "live" target
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")
    httpd, live_url = _serve(tmp_path)
    dead_url = "http://127.0.0.1:1"   # nothing listening here
    try:
        art = DeployResult(
            url=live_url,
            urls={"web": live_url, "api": dead_url},
            health_paths={"web": "/", "api": "/health"},
        )
        res = PhaseResult("deploy", 0, output_artifact=art)
        assert DeployGate().check(res).ok is False
    finally:
        httpd.shutdown()


def test_deploy_starts_datastore_before_backend_and_injects_conn_env(monkeypatch):
    order = []
    captured_env = {}

    def fake_start_service(target, network=None):
        order.append(("service", target.name, network))
        return f"devagent-preview-{target.name}"

    def fake_start_target(workdir, target, network=None, env=None):
        order.append(("target", target.name, network))
        captured_env[target.name] = env or {}
        return f"http://127.0.0.1:90/{target.name}"

    from devagent import deploy as deploy_mod
    monkeypatch.setattr(deploy_mod, "ensure_network", lambda name: None)

    scope = ProjectScope(title="A", targets=[
        ArtifactSpec(type="datastore", stack="postgres", name="db", acceptance_checks=[]),
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     detail={"datastore": "db", "conn_env": "DATABASE_URL"}, acceptance_checks=[]),
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web", acceptance_checks=[]),
    ])
    ctx = PhaseContext(sandbox=None, budget=None, ledger=None, artifacts={"scope": scope})
    res = DeployPhase(workdir="/out", start_service=fake_start_service,
                      start_target=fake_start_target).run(ctx)
    # datastore first, then backend, then frontend
    assert [o[0] for o in order] == ["service", "target", "target"]
    assert order[0][1] == "db" and order[1][1] == "api" and order[2][1] == "web"
    # backend received the resolved connection URL on the conn_env var
    assert captured_env["api"]["DATABASE_URL"] == "postgresql://devagent:devagent@db:5432/app"
    # datastore is recorded as a service, NOT as an HTTP url (so DeployGate won't probe it)
    assert "db" in res.output_artifact.services and "db" not in res.output_artifact.urls
    assert res.exit_code == 0


def test_start_service_argv_runs_image_with_volume_and_alias(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.setdefault("argvs", []).append(argv)
        return _Proc()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    monkeypatch.setattr(deploy, "_wait_service_ready", lambda *a, **k: True)
    from devagent.schema import ArtifactSpec
    cname = deploy.start_service(ArtifactSpec(type="datastore", stack="postgres", name="db",
                                              acceptance_checks=[]), network="net")
    run_argv = next(a for a in captured["argvs"] if "-d" in a)
    assert "postgres:16-alpine" in run_argv
    assert "--network-alias" in run_argv and "db" in run_argv
    assert any(str(a).endswith(":/var/lib/postgresql/data") for a in run_argv)
    assert cname == "devagent-preview-db"
