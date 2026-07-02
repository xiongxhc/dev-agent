from devagent.schema import IntegrationCheck, ServiceNode, SystemDesign
from devagent.tree import NodeResult, SUCCEEDED, FAILED
from devagent.integration import IntegrationReport
from devagent.system_build import build_system, SystemReport


def _design():
    return SystemDesign(
        title="Todo system",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="x")],
        integration_checks=[IntegrationCheck(service="api", route="/api/todos")])


def _architect(_prd):        # fake Architect: returns a SystemDesign directly
    return _design()


def test_all_green_reports_succeeded():
    torn = {"down": False}
    def bring_up(design):
        return {"api": "http://api"}, (lambda: torn.__setitem__("down", True))
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=lambda n, d: NodeResult(n.id, SUCCEEDED),
        bring_up=bring_up, architect=_architect,
        integration_runner=lambda checks, urls: IntegrationReport(
            steps=[{"service": "api", "route": "/api/todos", "ok": True, "detail": ""}]))
    assert isinstance(rep, SystemReport) and rep.status == "succeeded"
    assert rep.build_ok and rep.integration.ok
    assert torn["down"]                         # teardown always called


def test_build_failure_skips_bring_up():
    called = {"bring": False}
    def bring_up(design):
        called["bring"] = True; return {}, (lambda: None)
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=lambda n, d: NodeResult(n.id, FAILED, "boom"),
        bring_up=bring_up, architect=_architect,
        integration_runner=lambda c, u: IntegrationReport(steps=[]))
    assert rep.status == "build_failed" and not called["bring"]   # never bring up a broken build


def test_integration_failure_reports_and_tears_down():
    torn = {"down": False}
    def bring_up(design):
        return {"api": "http://api"}, (lambda: torn.__setitem__("down", True))
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=lambda n, d: NodeResult(n.id, SUCCEEDED),
        bring_up=bring_up, architect=_architect,
        integration_runner=lambda c, u: IntegrationReport(
            steps=[{"service": "api", "route": "/api/todos", "ok": False, "detail": "500"}]))
    assert rep.status == "integration_failed" and torn["down"]


def _design_dup_names():         # unique ids, colliding names — dirs/containers key on name
    return SystemDesign(
        title="Todo system",
        services=[ServiceNode(id="api1", name="api", kind="backend", stack="node-express",
                              prd_slice="x"),
                 ServiceNode(id="api2", name="api", kind="backend", stack="node-express",
                            prd_slice="y")])


def test_duplicate_service_names_is_design_failed():
    called = {"run_node": False, "bring_up": False}
    def run_node(n, d):
        called["run_node"] = True; return NodeResult(n.id, SUCCEEDED)
    def bring_up(design):
        called["bring_up"] = True; return {}, (lambda: None)
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=run_node, bring_up=bring_up, architect=lambda _prd: _design_dup_names())
    assert rep.status == "design_failed"
    assert not called["run_node"] and not called["bring_up"]
