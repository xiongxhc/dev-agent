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


def test_bring_up_collects_base_urls_in_topo_order(tmp_path):
    started = []
    def fake_start_target(out_dir, target, image=None, network=None, env=None):
        started.append(target.name)
        return f"http://{target.name}:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    base_urls, teardown = bring(_design())
    assert base_urls == {"api": "http://api:3000", "web": "http://web:3000"}
    assert started == ["api", "web"]           # topo order: api before web
    teardown()                                  # must not raise


def test_bring_up_omits_service_that_fails_to_start(tmp_path):
    def fake_start_target(out_dir, target, image=None, network=None, env=None):
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
    calls = []

    class _Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(system_build.subprocess, "run", fake_run)

    def fake_start_service(target, image=None, network=None):
        return f"devagent-preview-{target.name}"           # db starts successfully

    def fake_start_target(out_dir, target, image=None, network=None, env=None):
        return None if target.name == "web" else "http://api:3000"   # api starts, web fails

    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=fake_start_service, start_target=fake_start_target)
    _, teardown = bring(_design_with_datastore())
    teardown()

    rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
    net_calls = [c for c in calls if c[:2] == ["docker", "network"]]

    assert ["docker", "rm", "-f", "devagent-preview-db"] in rm_calls
    assert ["docker", "rm", "-f", "devagent-preview-api"] in rm_calls
    assert not any("devagent-preview-web" in c for c in rm_calls)  # web never started
    assert net_calls == [["docker", "network", "rm", f"devagent-sys-{tmp_path.name}"]]


def test_bring_up_mid_loop_exception_tears_down_and_propagates(tmp_path, monkeypatch):
    calls = []

    class _Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(system_build.subprocess, "run", fake_run)

    def fake_start_target(out_dir, target, image=None, network=None, env=None):
        if target.name == "web":            # crash on the 2nd service
            raise RuntimeError("boom")
        return "http://api:3000"

    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)

    with pytest.raises(RuntimeError, match="boom"):
        bring(_design())

    rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
    net_calls = [c for c in calls if c[:2] == ["docker", "network"]]

    assert ["docker", "rm", "-f", "devagent-preview-api"] in rm_calls  # already-started, removed
    assert not any("devagent-preview-web" in c for c in rm_calls)      # web never started
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
