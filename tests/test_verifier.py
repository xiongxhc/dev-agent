"""BuildVerifier — re-runs the build from source in a clean container. The docker run is
mocked, so no container / no tokens. Mirrors test_executor_sdk.py's seam-test style."""

import subprocess

from devagent import verifier as verifier_mod
from devagent.verifier import BuildVerifier, VerifyRequest


def _req(workdir):
    return VerifyRequest(workdir=str(workdir), run_id="r1")


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_verify_ok_when_build_exits_zero_and_dist_present(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        (out / "dist").mkdir(parents=True, exist_ok=True)
        (out / "dist" / "index.html").write_text("<html></html>")
        return _Proc(returncode=0, stdout="built")

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok is True
    assert rep.dist_present is True
    assert rep.exit_code == 0


def test_verify_fails_when_build_nonzero_exit(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        return _Proc(returncode=1, stderr="TS2304: Cannot find name 'foo'")

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok is False
    assert rep.exit_code == 1
    assert "TS2304" in rep.log_tail  # diagnostics preserved for the repair loop


def test_verify_uses_frozen_lockfile_and_carries_no_secret(tmp_path, monkeypatch):
    out = tmp_path / "out"
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc(returncode=0)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    BuildVerifier().verify(_req(out))
    joined = " ".join(captured["argv"])
    assert "--frozen-lockfile" in joined          # pinned-deps enforcement
    assert "ANTHROPIC_API_KEY" not in captured["argv"]  # verify needs no API key at all
    assert "sk-secret-value" not in joined


def test_verify_timeout_returns_failed_report(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier(timeout=1).verify(_req(out))
    assert rep.build_ok is False
    assert "tim" in (rep.error or "").lower()
