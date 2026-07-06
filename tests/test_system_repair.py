from devagent.integration import IntegrationReport
from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.system_build import (failing_steps, implicated_nodes,
                                    render_repair_context, SystemReport)


def _design():
    return SystemDesign(title="t", services=[
        ServiceNode(id="db", name="db", kind="datastore", stack="postgres", prd_slice="a db"),
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="x", depends_on=["db"]),
        ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="y", depends_on=["api"])])


def test_failing_steps_extracts_only_failures():
    report = IntegrationReport(steps=[
        {"service": "api", "route": "/todos", "ok": True, "detail": ""},
        {"service": "web", "route": "/", "ok": False, "detail": "500"}])
    assert failing_steps(report) == [{"service": "web", "route": "/", "detail": "500"}]


def test_implicated_nodes_maps_services_and_excludes_datastores():
    d = _design()
    steps = [{"service": "web", "route": "/", "detail": "500"},
             {"service": "api", "route": "/todos", "detail": "500"}]
    nodes = implicated_nodes(steps, d)
    # api precedes web in topo order; datastore-kind nodes are never implicated.
    assert [n.id for n in nodes] == ["api", "web"]


def test_implicated_nodes_dedups_and_skips_unknown_service():
    d = _design()
    steps = [{"service": "api", "route": "/a", "detail": ""},
             {"service": "api", "route": "/b", "detail": ""},
             {"service": "ghost", "route": "/x", "detail": ""}]
    assert [n.id for n in implicated_nodes(steps, d)] == ["api"]


def test_implicated_nodes_all_service_kind_is_empty():
    d = _design()
    steps = [{"service": "db", "route": "/", "detail": "no base_url for service 'db'"}]
    assert implicated_nodes(steps, d) == []


def test_render_repair_context_marks_the_target_nodes_steps():
    d = _design()
    report = IntegrationReport(steps=[
        {"service": "api", "route": "/todos", "ok": False, "detail": "500"},
        {"service": "web", "route": "/", "ok": True, "detail": ""}])
    node = next(n for n in d.services if n.id == "api")
    text = render_repair_context(report, node)
    assert "api" in text and "/todos" in text and "500" in text
    assert "web" in text                    # the WHOLE report, not just this node's steps
    api_line = next(ln for ln in text.splitlines() if "/todos" in ln)
    # NOTE: the brief's original generator (`" / " in ln or ln.rstrip().endswith("/")`) does
    # not match this render format when detail is empty (line is "...web /: ", which contains
    # neither " / " nor a trailing "/") -- see m23-task-4-report.md for the concern. Locate
    # the web step's line directly instead; "web" appears in exactly one line of this report.
    web_line = next(ln for ln in text.splitlines() if "web" in ln)
    assert api_line.startswith(">>>") and not web_line.startswith(">>>")


def test_system_report_repairs_defaults_empty():
    rep = SystemReport("t", {}, True, None, "succeeded")
    assert rep.repairs == []


from devagent.tree import NodeResult, SUCCEEDED, FAILED


def _design_api_provides():
    # The contract id must appear in the producer's `provides` or SystemDesign validation fails,
    # so declare it at construction — do NOT patch it on afterward.
    return SystemDesign(title="Todo", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="x", provides=["c"])],
        contracts=[Contract(id="c", kind="openapi", producer="api", spec={
            "paths": {"/todos": {"get": {"responses": {"200": {"content": {
                "application/json": {"schema": {"type": "array", "items": {
                    "type": "object", "properties": {"id": {"type": "integer"}}}}}}}}}}}})])


class FakeLedger:
    def __init__(self): self.events = []
    def append(self, ev): self.events.append(ev)


def _fail_then_pass_runner():
    """An integration runner that fails /todos the first call, passes it every call after."""
    calls = {"n": 0}
    def runner(checks, urls):
        calls["n"] += 1
        ok = calls["n"] > 1
        return IntegrationReport(steps=[{"service": "api", "route": "/todos",
                                         "ok": ok, "detail": "" if ok else "500"}])
    return runner, calls


def test_repair_success_reports_succeeded_with_loop_final_urls():
    from devagent.system_build import build_system
    d = _design_api_provides()
    runner, _ = _fail_then_pass_runner()
    ledger = FakeLedger()
    torn = {"n": 0}
    urls_seq = [{"api": "http://api-1"}, {"api": "http://api-2"}]
    bring = {"n": 0}
    def bring_up(design):
        u = urls_seq[min(bring["n"], 1)]; bring["n"] += 1
        return u, (lambda: torn.__setitem__("n", torn["n"] + 1))
    # run_node fires once per node during the initial build (repair_context=None) AND once per
    # repair (repair_context set) — count/assert ONLY on the repair invocations.
    repaired = {"n": 0}
    def run_node(node, design, repair_context=None):
        if repair_context is not None:
            repaired["n"] += 1
            assert "FAIL" in repair_context              # got the whole report
        return NodeResult(node.id, SUCCEEDED)

    rep = build_system("prd.md", budget=None, ledger=ledger,
                       run_node=run_node, bring_up=bring_up,
                       architect=lambda _p: d, integration_runner=runner,
                       max_system_repairs=1)
    assert rep.status == "succeeded"
    assert rep.urls == {"api": "http://api-2"}          # loop's FINAL base_urls, not the stale first
    assert repaired["n"] == 1 and len(rep.repairs) == 1
    assert {"event": "system_deploy", "urls": {"api": "http://api-2"}} in ledger.events
    assert any(e["event"] == "system_repair_start" for e in ledger.events)
    assert any(e["event"] == "system_repair_end" for e in ledger.events)


def test_repair_exhausted_tears_down_and_reports_integration_failed():
    from devagent.system_build import build_system
    d = _design_api_provides()
    torn = {"n": 0}
    def bring_up(design):
        return {"api": "http://api"}, (lambda: torn.__setitem__("n", torn["n"] + 1))
    def runner(checks, urls):     # always fails
        return IntegrationReport(steps=[{"service": "api", "route": "/todos",
                                         "ok": False, "detail": "500"}])
    rep = build_system("prd.md", budget=None, ledger=None,
                       run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
                       bring_up=bring_up, architect=lambda _p: d,
                       integration_runner=runner, max_system_repairs=1)
    assert rep.status == "integration_failed"
    assert len(rep.repairs) == 1
    assert torn["n"] >= 1          # teardown ran on the exhausted exit — no leaked stack


def test_no_repairable_nodes_skips_repair_and_tears_down():
    from devagent.system_build import build_system
    # A single datastore-kind design: the only failing service maps to a service-kind node.
    d = SystemDesign(title="t", services=[
        ServiceNode(id="db", name="db", kind="datastore", stack="postgres", prd_slice="a db")])
    torn = {"n": 0}
    repaired = {"n": 0}          # counts REPAIR calls only (repair_context set)
    def run_node(node, design, repair_context=None):
        if repair_context is not None:
            repaired["n"] += 1
        return NodeResult(node.id, SUCCEEDED)
    def bring_up(design):
        return {}, (lambda: torn.__setitem__("n", torn["n"] + 1))
    def runner(checks, urls):
        return IntegrationReport(steps=[{"service": "db", "route": "/", "ok": False,
                                         "detail": "no base_url for service 'db'"}])
    rep = build_system("prd.md", budget=None, ledger=None,
                       run_node=run_node,
                       bring_up=bring_up, architect=lambda _p: d,
                       integration_runner=runner, max_system_repairs=2)
    assert rep.status == "integration_failed"
    assert repaired["n"] == 0 and torn["n"] >= 1   # no repair attempted, teardown ran


def test_bring_up_raise_mid_repair_reraises_after_teardown():
    from devagent.system_build import build_system
    d = _design_api_provides()
    torn = {"n": 0}
    bring = {"n": 0}
    def bring_up(design):
        bring["n"] += 1
        if bring["n"] == 2:        # the post-repair bring-up crashes
            raise RuntimeError("docker exploded")
        return {"api": "http://api"}, (lambda: torn.__setitem__("n", torn["n"] + 1))
    def runner(checks, urls):      # first pass fails -> triggers a repair
        return IntegrationReport(steps=[{"service": "api", "route": "/todos",
                                         "ok": False, "detail": "500"}])
    import pytest
    with pytest.raises(RuntimeError, match="docker exploded"):
        build_system("prd.md", budget=None, ledger=None,
                     run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
                     bring_up=bring_up, architect=lambda _p: d,
                     integration_runner=runner, max_system_repairs=1)
    assert torn["n"] >= 1          # teardown ran despite the raise


def test_security_verify_seam_drives_the_same_repair_path():
    # Guards the M24 seam: a NON-integration trigger (fixture security failing steps) must
    # drive attribution -> repair -> combined re-verify with no new code path.
    from devagent.system_build import build_system
    d = _design_api_provides()
    sec_calls = {"n": 0}
    def security_verify(design, base_urls):
        sec_calls["n"] += 1
        # gate on the first pass only; clean after the repair
        return [{"service": "api", "route": "/todos", "ok": False,
                 "detail": "mass-assignment: role=admin accepted"}] if sec_calls["n"] == 1 else []
    def runner(checks, urls):      # integration always green
        return IntegrationReport(steps=[{"service": "api", "route": "/todos",
                                         "ok": True, "detail": ""}])
    repaired = {"n": 0}
    def run_node(node, design, repair_context=None):
        if repair_context is not None:
            repaired["n"] += 1
            assert "role=admin" in repair_context  # security evidence reached the executor
        return NodeResult(node.id, SUCCEEDED)
    def bring_up(design):
        return {"api": "http://api"}, (lambda: None)
    rep = build_system("prd.md", budget=None, ledger=None,
                       run_node=run_node, bring_up=bring_up, architect=lambda _p: d,
                       integration_runner=runner, security_verify=security_verify,
                       max_system_repairs=1)
    assert rep.status == "succeeded" and repaired["n"] == 1 and sec_calls["n"] == 2


def test_m24_gating_finding_drives_repair_and_records_findings():
    """A gating security finding renders as a failing step M23 attributes to the node; the
    node's repair_context is the WHOLE report; a repaired app re-passes integration AND
    security via the same reverify."""
    from devagent.system_build import build_system
    d = _design_api_provides()

    sec = {"n": 0, "sink": []}
    def security_verify(design, base_urls):
        sec["n"] += 1
        if sec["n"] == 1:
            step = {"service": "api", "route": "/auth/register", "ok": False,
                    "detail": "mass-assignment role=admin accepted — strip role server-side"}
            sec["sink"].append(step)      # cli captures findings via a sink like this
            return [step]
        return []                          # clean after repair
    def runner(checks, urls):
        return IntegrationReport(steps=[{"service": "api", "route": "/todos",
                                         "ok": True, "detail": ""}])
    repaired = {"n": 0}
    def run_node(node, design, repair_context=None):
        if repair_context is not None:            # skip the initial-build invocation
            repaired["n"] += 1
            assert "role=admin" in repair_context # security evidence reached the executor
        return NodeResult(node.id, SUCCEEDED)
    def bring_up(design):
        return {"api": "http://api"}, (lambda: None)

    rep = build_system("prd.md", budget=None, ledger=None, run_node=run_node,
                       bring_up=bring_up, architect=lambda _p: d, integration_runner=runner,
                       security_verify=security_verify, max_system_repairs=1)
    assert rep.status == "succeeded" and repaired["n"] == 1
