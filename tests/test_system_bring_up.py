import json
from pathlib import Path

import pytest

from devagent import system_build
from devagent.schema import ServiceNode, SystemDesign
from devagent.system_build import make_bring_up


def _design():
    return SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express", prd_slice="x"),
        ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="y", depends_on=["api"])])


def _design_with_datastore():
    return SystemDesign(title="t", services=[
        ServiceNode(id="db", name="db", kind="datastore", stack="postgres", prd_slice="z"),
        ServiceNode(id="api", name="api", kind="backend", stack="node-express", prd_slice="x",
                    depends_on=["db"]),
        ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="y", depends_on=["api"])])


def _write_scope(run_dir, node_name, targets, dist_for=()):
    out = Path(run_dir) / "services" / node_name / "out"
    dev = out / ".devagent"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "scope.json").write_text(json.dumps({"title": node_name, "targets": targets}))
    for name in dist_for:
        (out / name / "dist").mkdir(parents=True, exist_ok=True)


def _scaffold(run_dir):
    _write_scope(run_dir, "api",
                 [{"name": "api", "type": "backend", "stack": "node-express", "detail": {}}])
    _write_scope(run_dir, "web",
                 [{"name": "web", "type": "frontend", "stack": "node-vite-react", "detail": {}}],
                 dist_for=("web",))


def test_bring_up_collects_base_urls_in_topo_order(tmp_path):
    _scaffold(tmp_path)
    started = []
    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        started.append(target.name)
        return f"http://{target.name}:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    base_urls, teardown = bring(_design())
    assert base_urls == {"api": "http://api:3000", "web": "http://web:3000"}
    assert started == ["api", "web"]           # topo order: api before web
    teardown()                                  # must not raise


def test_bring_up_omits_service_that_fails_to_start(tmp_path):
    _scaffold(tmp_path)
    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        return None if target.name == "web" else "http://api:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    base_urls, _ = bring(_design())
    assert base_urls == {"api": "http://api:3000"}   # web absent -> M17 fails its E2E steps


def test_bring_up_uses_per_run_network(tmp_path):
    seen = []
    bring = make_bring_up(tmp_path, ensure_network=lambda n: seen.append(n),
                          start_service=lambda *a, **k: "c",
                          start_target=lambda *a, **k: "http://x:3000")
    bring(_design())
    assert seen == [f"devagent-sys-{tmp_path.name}"]


def test_bring_up_teardown_removes_started_containers_and_network(tmp_path, monkeypatch):
    _scaffold(tmp_path)
    calls = []

    class _Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(system_build.subprocess, "run", fake_run)

    def fake_start_service(target, image=None, network=None, **kw):
        return f"devagent-preview-{target.name}"           # db starts successfully

    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        return None if target.name == "web" else "http://api:3000"   # api starts, web fails

    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=fake_start_service, start_target=fake_start_target)
    _, teardown = bring(_design_with_datastore())
    teardown()

    rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
    vol_calls = [c for c in calls if c[:2] == ["docker", "volume"]]
    net_calls = [c for c in calls if c[:2] == ["docker", "network"]]

    assert ["docker", "rm", "-f", "devagent-preview-db"] in rm_calls
    # api's container is node-namespaced: node "api", scope target "api"
    assert ["docker", "rm", "-f", "devagent-preview-api-api"] in rm_calls
    assert not any("web" in " ".join(c) for c in rm_calls)  # web never started
    assert ["docker", "volume", "rm", "devagent-preview-api-api-data"] in vol_calls
    assert net_calls == [["docker", "network", "rm", f"devagent-sys-{tmp_path.name}"]]


def test_bring_up_mid_loop_exception_tears_down_and_propagates(tmp_path, monkeypatch):
    _scaffold(tmp_path)
    calls = []

    class _Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(system_build.subprocess, "run", fake_run)

    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        if target.name == "web":            # crash on the 2nd service
            raise RuntimeError("boom")
        return "http://api:3000"

    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)

    with pytest.raises(RuntimeError, match="boom"):
        bring(_design())

    rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
    net_calls = [c for c in calls if c[:2] == ["docker", "network"]]

    assert ["docker", "rm", "-f", "devagent-preview-api-api"] in rm_calls  # already-started, removed
    assert not any("web" in " ".join(c) for c in rm_calls)                 # web never started
    assert net_calls == [["docker", "network", "rm", f"devagent-sys-{tmp_path.name}"]]


def test_bring_up_teardown_never_raises_without_docker(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("docker not installed")

    monkeypatch.setattr(system_build.subprocess, "run", fake_run)

    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "devagent-preview-db",
                          start_target=lambda *a, **k: "http://api:3000")
    _, teardown = bring(_design_with_datastore())
    teardown()  # must not raise


def test_bring_up_injects_design_level_conn_env(tmp_path):
    _scaffold(tmp_path)
    seen = {}
    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        seen[target.name] = env
        return f"http://{target.name}:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "devagent-preview-db",
                          start_target=fake_start_target)
    bring(_design_with_datastore())
    assert seen["api"] == {
        "DATABASE_URL": "postgresql://devagent:devagent@db:5432/app"}


def test_bring_up_wires_cross_node_frontend_api_base(tmp_path):
    _scaffold(tmp_path)
    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        return f"http://{target.name}:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    bring(_design())
    cfg = json.loads((Path(tmp_path) / "services" / "web" / "out" / "web" / "dist"
                      / "config.json").read_text())
    assert cfg == {"apiBase": "http://api:3000"}


def test_bring_up_mounts_actual_scope_target_names(tmp_path):
    # The api node's sub-run scoped its backend target as "server", not "api".
    _write_scope(tmp_path, "api",
                 [{"name": "server", "type": "backend", "stack": "node-express", "detail": {}}])
    seen = []
    def fake_start_target(out_dir, target, image=None, network=None, env=None, **kw):
        seen.append((target.name, kw.get("container_name")))
        return "http://server:3000"
    d = SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express", prd_slice="x")])
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    base_urls, _ = bring(d)
    assert base_urls == {"api": "http://server:3000"}
    assert seen == [("server", "devagent-preview-api-server")]


def test_bring_up_skips_node_without_scope_json(tmp_path):
    d = SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express", prd_slice="x")])
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c",
                          start_target=lambda *a, **k: "http://x")
    base_urls, teardown = bring(d)
    assert base_urls == {}
    teardown()
