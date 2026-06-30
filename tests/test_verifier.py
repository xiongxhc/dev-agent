"""BuildVerifier — rebuild-from-source + kind-dispatched acceptance, both in a clean
container. The docker runs are mocked (no container / no tokens). A successful rebuild is
followed by a second docker run for the acceptance runner; the fake dispatches on argv.

All tests seed a scope.json (one or more targets) under out/.devagent/ and place artifacts
under out/<name>/ matching the per-target structure introduced in M6.
"""

import json
import subprocess

from devagent import recipes
from devagent import verifier as verifier_mod
from devagent.verifier import BuildVerifier, VerifyRequest


def _req(workdir):
    return VerifyRequest(workdir=str(workdir), run_id="r1")


def _seed_scope(out, targets):
    """Write scope.json under out/.devagent/. targets is a list of dicts with name+stack."""
    dev = out / ".devagent"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "scope.json").write_text(json.dumps({"targets": targets}))


def _seed_artifact(out, name, glob_path):
    """Create the artifact file at out/<name>/<glob_path>."""
    artifact = out / name / glob_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("x")


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
    _seed_scope(out, [{"name": "web", "stack": "node-vite-react"}])

    def fake_run(argv, **kw):
        if _is_acceptance(argv):
            (out / ".devagent").mkdir(parents=True, exist_ok=True)
            (out / ".devagent" / "acceptance.json").write_text(json.dumps(
                {"checks": [{"kind": "route_status", "route": "/", "ok": True, "detail": "status 200"}],
                 "all_pass": True}))
            return _Proc(0)
        # rebuild run — create the artifact under out/web/
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc(0, stdout="built")

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok and rep.dist_present
    assert len(rep.checks) == 1 and rep.checks[0].ok
    assert rep.ok is True


def test_verify_fails_and_skips_acceptance_when_build_nonzero(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _seed_scope(out, [{"name": "web", "stack": "node-vite-react"}])
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
    _seed_scope(out, [{"name": "web", "stack": "node-vite-react"}])

    def fake_run(argv, **kw):
        if _is_acceptance(argv):
            (out / ".devagent").mkdir(parents=True, exist_ok=True)
            (out / ".devagent" / "acceptance.json").write_text(json.dumps(
                {"checks": [{"kind": "selector_present", "route": "/", "ok": False,
                             "detail": "selector '#hero' missing"}], "all_pass": False}))
            return _Proc(0)
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc(0)

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier().verify(_req(out))
    assert rep.build_ok and rep.dist_present     # the build itself is fine
    assert rep.ok is False                        # but acceptance failed
    assert "ACCEPTANCE FAILURES" in rep.log_tail and "#hero" in rep.log_tail


def test_rebuild_uses_frozen_lockfile_and_carries_no_secret(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _seed_scope(out, [{"name": "web", "stack": "node-vite-react"}])
    argvs = []

    def fake_run(argv, **kw):
        argvs.append(argv)
        if _is_acceptance(argv):
            (out / ".devagent").mkdir(parents=True, exist_ok=True)
            (out / ".devagent" / "acceptance.json").write_text('{"checks": [], "all_pass": false}')
            return _Proc(0)
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc(0)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    BuildVerifier().verify(_req(out))
    rebuild = next(a for a in argvs if not _is_acceptance(a))
    assert "--frozen-lockfile" in recipes.get("node-vite-react").build_cmd  # pinned-deps at recipe level
    assert "--frozen-lockfile" in " ".join(rebuild)         # pinned-deps enforcement
    for argv in argvs:
        assert "ANTHROPIC_API_KEY" not in argv               # verify needs no API key
        assert "sk-secret-value" not in " ".join(argv)


def test_verify_timeout_returns_failed_report(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _seed_scope(out, [{"name": "web", "stack": "node-vite-react"}])

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(verifier_mod.subprocess, "run", fake_run)
    rep = BuildVerifier(timeout=1).verify(_req(out))
    assert rep.build_ok is False
    assert "tim" in (rep.error or "").lower()


def test_verify_brings_up_datastore_and_injects_conn_env(tmp_path):
    dev = tmp_path / ".devagent"
    dev.mkdir(parents=True)
    (dev / "scope.json").write_text(json.dumps({"targets": [
        {"name": "api", "stack": "node-express", "kind": "build",
         "detail": {"datastore": "db", "conn_env": "DATABASE_URL"}},
        {"name": "db", "stack": "postgres", "kind": "service", "detail": {}},
    ]}))
    (tmp_path / "api" / "dist").mkdir(parents=True)
    (tmp_path / "api" / "dist" / "server.js").write_text("x")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "/acceptance.py" in argv:
            (tmp_path / ".devagent" / "acceptance.json").write_text(
                '{"checks":[{"kind":"persistence_survives_restart","route":"/api/tasks",'
                '"ok":true,"detail":"present"}],"all_pass":true}')
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})

    rep = BuildVerifier(runner=fake_run, network="devagent-egress",
                        proxy_url="http://devagent-proxy:3128").verify(
        VerifyRequest(workdir=str(tmp_path), run_id="r1"))
    flat = [" ".join(map(str, a)) for a in calls]
    # datastore started detached with its image, a named volume, and an alias == target name
    assert any("run" in f and "-d" in f and "postgres:16-alpine" in f
               and "--network-alias" in f and "db" in f for f in flat)
    assert any("pg_isready" in f for f in flat)                      # readiness poll via docker exec
    # acceptance container received the resolved connection URL
    accept = next(f for f in flat if "/acceptance.py" in f)
    assert "DATABASE_URL=postgresql://devagent:devagent@db:5432/app" in accept
    # teardown removed the datastore container AND its named volume
    assert any(f.startswith("docker rm -f") and "r1" in f for f in flat) or \
           any("rm" in f and "-f" in f and "r1" in f for f in flat)
    assert any("volume" in f and "rm" in f for f in flat)
    assert rep.ok is True
    # the service target was NOT rebuilt (no `sh -c` build for it)
    assert not any("/out/db" in f for f in flat)


def test_verify_injects_idp_issuer_env_for_federated_auth(tmp_path):
    """A backend declaring detail.idp gets <idp_env>=<issuer-url> injected from the mock-IdP
    service (the federated-auth seam reuses the datastore service mechanism — M10)."""
    dev = tmp_path / ".devagent"
    dev.mkdir(parents=True)
    # reuse the postgres service recipe as a stand-in service; the injection is recipe-agnostic.
    (dev / "scope.json").write_text(json.dumps({"targets": [
        {"name": "api", "stack": "node-express", "kind": "build",
         "detail": {"idp": "idp", "idp_env": "OIDC_ISSUER"}},
        {"name": "idp", "stack": "postgres", "kind": "service", "detail": {}},
    ]}))
    (tmp_path / "api" / "dist").mkdir(parents=True)
    (tmp_path / "api" / "dist" / "server.js").write_text("x")

    def fake_run(argv, **kw):
        if "/acceptance.py" in argv:
            (tmp_path / ".devagent" / "acceptance.json").write_text(
                '{"checks":[{"kind":"route_status","route":"/me","ok":true,"detail":""}],"all_pass":true}')
        fake_run.calls.append(argv)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})
    fake_run.calls = []

    BuildVerifier(runner=fake_run, network="devagent-egress",
                  proxy_url="http://devagent-proxy:3128").verify(
        VerifyRequest(workdir=str(tmp_path), run_id="r1"))
    accept = next(" ".join(map(str, a)) for a in fake_run.calls if "/acceptance.py" in a)
    assert "OIDC_ISSUER=postgresql://devagent:devagent@idp:5432/app" in accept


def test_verify_fails_clearly_when_idp_dep_names_no_service(tmp_path):
    """A backend declaring detail.idp pointing at a non-service target fails with a clear error
    rather than silently booting the app with OIDC_ISSUER unset."""
    dev = tmp_path / ".devagent"
    dev.mkdir(parents=True)
    (dev / "scope.json").write_text(json.dumps({"targets": [
        {"name": "api", "stack": "node-express", "kind": "build",
         "detail": {"idp": "ghost"}},                 # no service named "ghost"
        {"name": "db", "stack": "postgres", "kind": "service", "detail": {}},
    ]}))
    (tmp_path / "api" / "dist").mkdir(parents=True)
    (tmp_path / "api" / "dist" / "server.js").write_text("x")

    def fake_run(argv, **kw):
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})

    rep = BuildVerifier(runner=fake_run, network="devagent-egress",
                        proxy_url="http://devagent-proxy:3128").verify(
        VerifyRequest(workdir=str(tmp_path), run_id="r1"))
    assert rep.ok is False
    assert "ghost" in (rep.error or "") and "idp" in (rep.error or "")


def test_verifier_loops_targets_and_aggregates(tmp_path, monkeypatch):
    # scope.json with two targets
    dev = tmp_path / ".devagent"
    dev.mkdir(parents=True)
    (dev / "scope.json").write_text('{"targets":['
        '{"name":"web","stack":"node-vite-react"},'
        '{"name":"api","stack":"node-express"}]}')
    # fake every container run as success; create the expected artifacts
    (tmp_path / "web" / "dist").mkdir(parents=True)
    (tmp_path / "web" / "dist" / "index.html").write_text("x")
    (tmp_path / "api" / "dist").mkdir(parents=True)
    (tmp_path / "api" / "dist" / "server.js").write_text("x")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if _is_acceptance(argv):
            (tmp_path / ".devagent" / "acceptance.json").write_text(
                '{"checks":[{"kind":"route_status","route":"/","ok":true,"detail":"200"}],'
                '"all_pass":true}')
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})

    v = BuildVerifier(runner=fake_run)
    rep = v.verify(VerifyRequest(workdir=str(tmp_path), run_id="r"))
    assert rep.build_ok and rep.dist_present
    # one rebuild per target (2 sh -c calls) + one acceptance run
    rebuild_calls = [c for c in calls if not _is_acceptance(c)]
    assert len(rebuild_calls) == 2
    # each rebuild uses the correct working dir
    web_call = next(c for c in rebuild_calls if "/out/web" in " ".join(c))
    api_call = next(c for c in rebuild_calls if "/out/api" in " ".join(c))
    assert "frozen-lockfile" in " ".join(web_call)
    assert "frozen-lockfile" in " ".join(api_call)
    assert rep.ok is True


def test_verify_creates_per_run_network_when_egress_disabled(tmp_path):
    # Seed scope with a build target (api) and a service target (db).
    dev = tmp_path / ".devagent"
    dev.mkdir(parents=True)
    (dev / "scope.json").write_text(json.dumps({"targets": [
        {"name": "api", "stack": "node-express", "kind": "build",
         "detail": {"datastore": "db", "conn_env": "DATABASE_URL"}},
        {"name": "db", "stack": "postgres", "kind": "service", "detail": {}},
    ]}))
    # Create the artifact.
    (tmp_path / "api" / "dist").mkdir(parents=True)
    (tmp_path / "api" / "dist" / "server.js").write_text("x")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "/acceptance.py" in argv:
            (tmp_path / ".devagent" / "acceptance.json").write_text(
                '{"checks":[{"kind":"persistence_survives_restart","route":"/api/tasks",'
                '"ok":true,"detail":"present"}],"all_pass":true}')
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})

    # BuildVerifier with NO network (egress disabled) — forces per-run bridge network.
    rep = BuildVerifier(runner=fake_run).verify(
        VerifyRequest(workdir=str(tmp_path), run_id="r1"))
    flat = [" ".join(map(str, a)) for a in calls]

    # 1. Per-run bridge network created before service startup.
    assert any("docker" in f and "network" in f and "create" in f
               and "devagent-verify-r1" in f for f in flat), \
        f"Expected 'docker network create devagent-verify-r1' in calls: {flat}"

    # 2. Datastore (postgres) runs on the per-run network, not an egress network.
    ds_run = next((f for f in flat if "run" in f and "-d" in f and "postgres:16-alpine" in f),
                  None)
    assert ds_run is not None, f"Expected postgres docker run in: {flat}"
    assert "--network devagent-verify-r1" in ds_run, \
        f"Expected '--network devagent-verify-r1' in datastore run: {ds_run}"
    assert "--network-alias" in ds_run and "db" in ds_run, \
        f"Expected network alias 'db' in datastore run: {ds_run}"

    # 3. Acceptance runs on the per-run network with NO proxy (egress disabled).
    accept = next((f for f in flat if "/acceptance.py" in f), None)
    assert accept is not None, f"Expected acceptance run in: {flat}"
    assert "--network devagent-verify-r1" in accept, \
        f"Expected '--network devagent-verify-r1' in acceptance: {accept}"
    assert "HTTPS_PROXY" not in accept and "HTTP_PROXY" not in accept, \
        f"Expected no proxy env in acceptance (egress disabled): {accept}"
    # But connection env should be injected.
    assert "DATABASE_URL=postgresql://devagent:devagent@db:5432/app" in accept, \
        f"Expected DATABASE_URL in acceptance: {accept}"

    # 4. Per-run network torn down in cleanup.
    assert any("docker" in f and "network" in f and "rm" in f
               and "devagent-verify-r1" in f for f in flat), \
        f"Expected 'docker network rm devagent-verify-r1' in calls: {flat}"

    # 5. Overall result is pass.
    assert rep.ok is True
