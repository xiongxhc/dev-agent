import threading

import pytest

from devagent.schema import ServiceNode, SystemDesign
from devagent.tree import (
    BLOCKED,
    FAILED,
    SUCCEEDED,
    NodeResult,
    SystemBuildResult,
    TreeOrchestrator,
    topo_order,
)


def _svc(id, deps=None):
    return ServiceNode(id=id, name=id, kind="backend", stack="node-express",
                       prd_slice="x", depends_on=deps or [])


def _design(*edges):
    """edges: (id, [deps]) tuples."""
    return SystemDesign(title="t", services=[_svc(i, d) for i, d in edges])


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


def test_topo_order_raises_on_unresolvable_deps():
    bad = SystemDesign.model_construct(
        title="t",
        services=[ServiceNode(id="a", name="a", kind="backend", stack="node-express",
                              prd_slice="x", depends_on=["ghost"])],
        contracts=[], version=1)
    with pytest.raises(ValueError):
        topo_order(bad)


def test_all_succeed_when_run_node_succeeds():
    d = _design(("api", []), ("web", ["api"]))
    orch = TreeOrchestrator(run_node=lambda n, dz: NodeResult(n.id, SUCCEEDED))
    res = orch.run(d)
    assert res.status == SUCCEEDED
    assert {k: v.status for k, v in res.results.items()} == {"api": SUCCEEDED, "web": SUCCEEDED}


def test_dependent_is_blocked_when_producer_fails():
    d = _design(("api", []), ("web", ["api"]), ("worker", []))
    def run_node(n, dz):
        return NodeResult(n.id, FAILED if n.id == "api" else SUCCEEDED)
    res = TreeOrchestrator(run_node=run_node).run(d)
    assert res.results["api"].status == FAILED
    assert res.results["web"].status == BLOCKED      # consumer of failed producer
    assert res.results["worker"].status == SUCCEEDED  # independent -> still runs
    assert res.status == FAILED


def test_blocking_is_transitive():
    d = _design(("a", []), ("b", ["a"]), ("c", ["b"]))
    def run_node(n, dz):
        return NodeResult(n.id, FAILED if n.id == "a" else SUCCEEDED)
    res = TreeOrchestrator(run_node=run_node).run(d)
    assert res.results["a"].status == FAILED
    assert res.results["b"].status == BLOCKED
    assert res.results["c"].status == BLOCKED        # blocked transitively through b


def test_run_node_never_called_for_blocked_nodes():
    d = _design(("api", []), ("web", ["api"]))
    called = []
    def run_node(n, dz):
        called.append(n.id)
        return NodeResult(n.id, FAILED if n.id == "api" else SUCCEEDED)
    TreeOrchestrator(run_node=run_node).run(d)
    assert called == ["api"]                          # web never invoked


def test_independent_nodes_run_concurrently():
    # two independent nodes; both must be in run_node simultaneously if concurrency>=2
    d = _design(("a", []), ("b", []))
    barrier = threading.Barrier(2, timeout=5)
    def run_node(n, dz):
        barrier.wait()                                # deadlocks/timeouts unless run in parallel
        return NodeResult(n.id, SUCCEEDED)
    res = TreeOrchestrator(run_node=run_node, concurrency=2).run(d)
    assert res.status == SUCCEEDED


def test_ledger_records_node_events():
    d = _design(("api", []))
    events = []
    class L:
        def append(self, e): events.append(e)
    TreeOrchestrator(run_node=lambda n, dz: NodeResult(n.id, SUCCEEDED), ledger=L()).run(d)
    kinds = [e.get("event") for e in events]
    assert "system_build_start" in kinds and "node" in kinds and "system_build_end" in kinds
