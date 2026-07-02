from pathlib import Path
from devagent.schema import ServiceNode, SystemDesign
from devagent.tree import NodeResult, SUCCEEDED, FAILED
from devagent.system_build import make_run_node


def _design():
    return SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express", prd_slice="A todo API.")])


def test_run_node_writes_prd_and_maps_success(tmp_path):
    seen = {}
    def fake_build_service(node, svc_dir, budget, ledger):
        seen["dir"] = svc_dir
        seen["prd"] = (Path(svc_dir) / "prd.md").read_text()
        return "succeeded"
    rn = make_run_node(tmp_path, budget=object(), ledger=None, build_service=fake_build_service)
    d = _design()
    res = rn(d.services[0], d)
    assert isinstance(res, NodeResult) and res.status == SUCCEEDED
    assert seen["prd"] == "A todo API."
    assert seen["dir"].endswith("services/api")


def test_run_node_maps_failure(tmp_path):
    rn = make_run_node(tmp_path, budget=object(), ledger=None,
                       build_service=lambda *a: "failed")
    d = _design()
    assert rn(d.services[0], d).status == FAILED


def test_run_node_exception_becomes_failed(tmp_path):
    def boom(*a): raise RuntimeError("kaboom")
    rn = make_run_node(tmp_path, budget=object(), ledger=None, build_service=boom)
    d = _design()
    res = rn(d.services[0], d)
    assert res.status == FAILED and "kaboom" in res.detail
