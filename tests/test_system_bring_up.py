from devagent.schema import ServiceNode, SystemDesign
from devagent.system_build import make_bring_up


def _design():
    return SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express", prd_slice="x"),
        ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="y", depends_on=["api"])])


def test_bring_up_collects_base_urls_in_topo_order(tmp_path):
    started = []
    def fake_start_target(out_dir, target, image=None, network=None, env=None):
        started.append(target["name"])
        return f"http://{target['name']}:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    base_urls, teardown = bring(_design())
    assert base_urls == {"api": "http://api:3000", "web": "http://web:3000"}
    assert started == ["api", "web"]           # topo order: api before web
    teardown()                                  # must not raise


def test_bring_up_omits_service_that_fails_to_start(tmp_path):
    def fake_start_target(out_dir, target, image=None, network=None, env=None):
        return None if target["name"] == "web" else "http://api:3000"
    bring = make_bring_up(tmp_path, ensure_network=lambda n: None,
                          start_service=lambda *a, **k: "c", start_target=fake_start_target)
    base_urls, _ = bring(_design())
    assert base_urls == {"api": "http://api:3000"}   # web absent -> M17 fails its E2E steps
