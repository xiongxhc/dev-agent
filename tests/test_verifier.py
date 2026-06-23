"""BuildVerifier — rebuild-from-source + kind-dispatched acceptance, both in a clean
container. The docker runs are mocked (no container / no tokens). A successful rebuild is
followed by a second docker run for the acceptance runner; the fake dispatches on argv."""

import json
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


def _is_acceptance(argv):
    # Exact element match — the acceptance run has "/acceptance.py" as the python target.
    # (Substring matching is fragile: pytest's tmp_path can contain the test name.)
    return "/acceptance.py" in argv


def test_verify_ok_when_build_green_and_acceptance_passes(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        if _is_acceptance(argv):
            (out / ".devagent").mkdir(parents=True, exist_ok=True)
            (out / ".devagent" / "acceptance.json").write_text(json.dumps(
                {"checks": [{"kind": "route_status", "route": "/", "ok": True, "detail": "status 200"}],
                 "all_pass": True}))
            return _Proc(0)
        (out / "dist").mkdir(parents=True, exist_ok=True)
        (out / "dist" / "index.html").write_text("<html></html>")
        return _Proc(0, stdout="built")

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok and rep.dist_present
    assert len(rep.checks) == 1 and rep.checks[0].ok
    assert rep.ok is True


def test_verify_fails_and_skips_acceptance_when_build_nonzero(tmp_path, monkeypatch):
    out = tmp_path / "out"
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _Proc(1, stderr="TS2304: Cannot find name 'foo'")

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok is False and rep.ok is False
    assert "TS2304" in rep.log_tail
    assert not any(_is_acceptance(a) for a in calls)  # acceptance never ran


def test_failed_acceptance_check_surfaces_for_repair(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        if _is_acceptance(argv):
            (out / ".devagent").mkdir(parents=True, exist_ok=True)
            (out / ".devagent" / "acceptance.json").write_text(json.dumps(
                {"checks": [{"kind": "selector_present", "route": "/", "ok": False,
                             "detail": "selector '#hero' missing"}], "all_pass": False}))
            return _Proc(0)
        (out / "dist").mkdir(parents=True, exist_ok=True)
        (out / "dist" / "index.html").write_text("<html></html>")
        return _Proc(0)

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok and rep.dist_present     # the build itself is fine
    assert rep.ok is False                        # but acceptance failed
    assert "ACCEPTANCE FAILURES" in rep.log_tail and "#hero" in rep.log_tail


def test_rebuild_uses_frozen_lockfile_and_carries_no_secret(tmp_path, monkeypatch):
    out = tmp_path / "out"
    argvs = []

    def fake_run(argv, **kw):
        argvs.append(argv)
        if _is_acceptance(argv):
            (out / ".devagent").mkdir(parents=True, exist_ok=True)
            (out / ".devagent" / "acceptance.json").write_text('{"checks": [], "all_pass": false}')
            return _Proc(0)
        (out / "dist").mkdir(parents=True, exist_ok=True)
        (out / "dist" / "index.html").write_text("<html></html>")
        return _Proc(0)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    BuildVerifier().verify(_req(out))
    rebuild = next(a for a in argvs if not _is_acceptance(a))
    assert "--frozen-lockfile" in " ".join(rebuild)         # pinned-deps enforcement
    for argv in argvs:
        assert "ANTHROPIC_API_KEY" not in argv               # verify needs no API key
        assert "sk-secret-value" not in " ".join(argv)


def test_verify_timeout_returns_failed_report(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier(timeout=1).verify(_req(out))
    assert rep.build_ok is False
    assert "tim" in (rep.error or "").lower()
