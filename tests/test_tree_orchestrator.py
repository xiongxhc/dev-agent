from devagent.schema import ServiceNode, SystemDesign
from devagent.tree import NodeResult, SystemBuildResult, topo_order


def _svc(id, deps=None):
    return ServiceNode(id=id, name=id, kind="backend", stack="node-express",
                       prd_slice="x", depends_on=deps or [])


def test_topo_order_puts_dependencies_first():
    d = SystemDesign(title="t", services=[_svc("web", ["api"]), _svc("api")])
    order = topo_order(d)
    assert order.index("api") < order.index("web")


def test_topo_order_deterministic_ties_by_declaration_order():
    d = SystemDesign(title="t", services=[_svc("a"), _svc("b"), _svc("c")])
    assert topo_order(d) == ["a", "b", "c"]


def test_node_result_and_system_result_construct():
    nr = NodeResult(node_id="api", status="succeeded")
    assert nr.detail == ""
    sr = SystemBuildResult(results={"api": nr}, status="succeeded", order=["api"])
    assert sr.results["api"].status == "succeeded"
