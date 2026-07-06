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


def test_all_green_reports_succeeded_and_keeps_preview_up():
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
    # One-flow: success KEEPS the brought-up system as the preview (like a single-run
    # deploy) and reports its URLs; teardown happens only on failure.
    assert not torn["down"]
    assert rep.urls == {"api": "http://api"}


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
        # Task 3 made run_node 3-arg (repair_context). This design's api node is kind=build
        # (repairable), so under the M23 loop (default max_system_repairs=1) integration
        # failure fires one repair — which the always-failing runner leaves failing, so the
        # terminal verdict is still integration_failed + teardown. Accept the repair_context
        # kwarg the loop passes; assertions unchanged.
        run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
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


def test_build_system_persists_design_json(tmp_path):
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=lambda n, d: NodeResult(n.id, SUCCEEDED),
        bring_up=lambda d: ({"api": "http://api"}, lambda: None),
        architect=_architect, run_dir=tmp_path,
        integration_runner=lambda checks, urls: IntegrationReport(
            steps=[{"service": "api", "route": "/api/todos", "ok": True, "detail": ""}]))
    assert rep.status == "succeeded"
    import json
    design = json.loads((tmp_path / "design.json").read_text())
    assert design["title"] == "Todo system"
    assert design["services"][0]["id"] == "api"


def _design_with_contract():
    from devagent.schema import Contract
    return SystemDesign(
        title="Todo system",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="x", provides=["openapi_todos"])],
        contracts=[Contract(id="openapi_todos", kind="openapi", producer="api", spec={
            "paths": {"/todos": {"get": {"responses": {"200": {"content": {
                "application/json": {"schema": {"type": "array", "items": {
                    "type": "object", "properties": {"id": {"type": "integer"}}}}}}}}}}}})],
        integration_checks=[IntegrationCheck(service="api", route="/WRONG-llm-invented")])


def test_integration_checks_are_derived_from_contracts_not_architect_prose():
    # One-flow: the contract is the single check authority — the architect's free-written
    # integration_checks contradicted it in live runs, so the gate runs derived checks.
    seen = {}
    def runner(checks, urls):
        seen["routes"] = [c.route for c in checks]
        return IntegrationReport(steps=[{"service": "api", "route": r, "ok": True, "detail": ""}
                                        for r in seen["routes"]])
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=lambda n, d: NodeResult(n.id, SUCCEEDED),
        bring_up=lambda d: ({"api": "http://api"}, lambda: None),
        architect=lambda _prd: _design_with_contract(), integration_runner=runner)
    assert rep.status == "succeeded"
    assert seen["routes"] == ["/todos"]
    assert "/WRONG-llm-invented" not in seen["routes"]


def test_integration_falls_back_to_architect_checks_when_nothing_derivable():
    # No contracts -> nothing to derive -> the architect's checks are better than zero steps
    # (IntegrationGate fails an empty report).
    seen = {}
    def runner(checks, urls):
        seen["routes"] = [c.route for c in checks]
        return IntegrationReport(steps=[{"service": "api", "route": r, "ok": True, "detail": ""}
                                        for r in seen["routes"]])
    rep = build_system(
        "prd.md", budget=None, ledger=None,
        run_node=lambda n, d: NodeResult(n.id, SUCCEEDED),
        bring_up=lambda d: ({"api": "http://api"}, lambda: None),
        architect=_architect, integration_runner=runner)
    assert rep.status == "succeeded"
    assert seen["routes"] == ["/api/todos"]


def test_ledger_system_build_end_carries_post_integration_status():
    # Review finding (2026-07-03): the ledger ended "succeeded" (tree verdict) while
    # integration then failed. The final system_build_end must carry the true status.
    class FakeLedger:
        def __init__(self): self.events = []
        def append(self, ev): self.events.append(ev)
    ledger = FakeLedger()
    rep = build_system(
        "prd.md", budget=None, ledger=ledger,
        # 3-arg run_node (Task 3): the repairable api node makes the M23 loop fire one repair
        # on integration failure; the runner keeps failing so the final system_build_end still
        # carries integration_failed (what this test asserts). Signature updated, assertions kept.
        run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
        bring_up=lambda d: ({"api": "http://api"}, lambda: None),
        architect=_architect,
        integration_runner=lambda c, u: IntegrationReport(
            steps=[{"service": "api", "route": "/api/todos", "ok": False, "detail": "500"}]))
    assert rep.status == "integration_failed"
    ends = [e for e in ledger.events if e["event"] == "system_build_end"]
    assert ends == [{"event": "system_build_end", "status": "integration_failed"}]
    tree_ends = [e for e in ledger.events if e["event"] == "tree_build_end"]
    assert tree_ends == [{"event": "tree_build_end", "status": "succeeded"}]


def test_success_logs_system_deploy_urls():
    class FakeLedger:
        def __init__(self): self.events = []
        def append(self, ev): self.events.append(ev)
    ledger = FakeLedger()
    rep = build_system(
        "prd.md", budget=None, ledger=ledger,
        run_node=lambda n, d: NodeResult(n.id, SUCCEEDED),
        bring_up=lambda d: ({"api": "http://api"}, lambda: None),
        architect=_architect,
        integration_runner=lambda c, u: IntegrationReport(
            steps=[{"service": "api", "route": "/api/todos", "ok": True, "detail": ""}]))
    assert rep.status == "succeeded"
    assert {"event": "system_deploy", "urls": {"api": "http://api"}} in ledger.events
    assert ledger.events[-1] == {"event": "system_build_end", "status": "succeeded"}
