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
