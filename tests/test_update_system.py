"""M25 update_system orchestration tests — injected fakes, no docker/tokens."""
from pathlib import Path

from devagent.integration import IntegrationReport
from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.system_build import update_system
from devagent.tree import NodeResult, SUCCEEDED


class _Ledger:
    def __init__(self):
        self.events = []

    def append(self, ev):
        self.events.append(ev)


def _design(web_slice="a UI", tables=None, services_extra=()):
    tables = tables or {"todos": ["id", "title"]}
    return SystemDesign(
        title="Todos",
        services=[
            ServiceNode(id="db", name="db", kind="datastore", stack="postgres",
                        prd_slice="store", provides=["db.schema"]),
            ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                        prd_slice="a JSON API", depends_on=["db"], provides=["api.openapi"],
                        consumes=["db.schema"]),
            ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                        prd_slice=web_slice, depends_on=["api"], consumes=["api.openapi"]),
            *services_extra,
        ],
        contracts=[
            Contract(id="db.schema", kind="db_schema", producer="db",
                     spec={"tables": tables}),
            Contract(id="api.openapi", kind="openapi", producer="api",
                     spec={"paths": {"/api/todos": {"get": {}}}}),
        ])


def _setup(tmp_path, prior=None):
    (tmp_path / "design.json").write_text((prior or _design()).model_dump_json(indent=2))
    change = tmp_path / "change.md"
    change.write_text("make the buttons blue")
    return change


def _ok_runner(checks, urls):
    return IntegrationReport(steps=[{"service": "web", "route": "/", "ok": True, "detail": ""}])


def _urls(design):
    return {s.id: f"http://{s.name}" for s in design.services}


def test_only_changed_services_rebuild_and_unchanged_are_reused(tmp_path):
    change = _setup(tmp_path)
    built = []
    def run_node(node, design, repair_context=None):
        built.append(node.id)
        return NodeResult(node.id, SUCCEEDED)
    rep = update_system(
        tmp_path, str(change), budget=None, ledger=None, run_node=run_node,
        bring_up_factory=lambda preserve: (lambda design: (_urls(design), lambda: None)),
        architect=lambda _c, _p: _design(web_slice="a UI with blue buttons"),
        integration_runner=_ok_runner)
    assert rep.status == "succeeded"
    assert built == ["web"]                                  # api and db never rebuilt
    assert rep.node_results["api"].detail.startswith("unchanged")


def test_code_only_change_preserves_data(tmp_path):
    change = _setup(tmp_path)
    preserve = {}
    def factory(p):
        preserve["flag"] = p
        return lambda design: (_urls(design), lambda: None)
    ledger = _Ledger()
    update_system(tmp_path, str(change), budget=None, ledger=ledger,
                  run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
                  bring_up_factory=factory,
                  architect=lambda _c, _p: _design(web_slice="new UI"),
                  integration_runner=_ok_runner)
    assert preserve["flag"] is True
    start = next(e for e in ledger.events if e["event"] == "system_update_start")
    assert start["schema_changed"] is False and start["changed"] == ["web"]


def test_schema_change_resets_data_and_ledger_says_so(tmp_path):
    change = _setup(tmp_path)
    preserve = {}
    def factory(p):
        preserve["flag"] = p
        return lambda design: (_urls(design), lambda: None)
    ledger = _Ledger()
    update_system(tmp_path, str(change), budget=None, ledger=ledger,
                  run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
                  bring_up_factory=factory,
                  architect=lambda _c, _p: _design(tables={"todos": ["id", "title", "done"]}),
                  integration_runner=_ok_runner)
    assert preserve["flag"] is False
    start = next(e for e in ledger.events if e["event"] == "system_update_start")
    assert start["schema_changed"] is True


def test_repair_pass_reaches_run_node_even_for_unchanged_service(tmp_path):
    change = _setup(tmp_path)
    calls = []
    def run_node(node, design, repair_context=None):
        calls.append((node.id, repair_context is not None))
        return NodeResult(node.id, SUCCEEDED)
    def failing_runner(checks, urls):    # always blames the UNCHANGED api
        return IntegrationReport(steps=[
            {"service": "api", "route": "/api/todos", "ok": False, "detail": "500"}])
    rep = update_system(
        tmp_path, str(change), budget=None, ledger=None, run_node=run_node,
        bring_up_factory=lambda p: (lambda design: (_urls(design), lambda: None)),
        architect=lambda _c, _p: _design(web_slice="new UI"),
        integration_runner=failing_runner, max_system_repairs=1)
    assert rep.status == "integration_failed"
    assert ("api", True) in calls        # M23 repair pierced the unchanged-shortcut


def test_update_persists_the_new_design_json(tmp_path):
    change = _setup(tmp_path)
    new = _design(web_slice="a UI with blue buttons")
    update_system(tmp_path, str(change), budget=None, ledger=None,
                  run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
                  bring_up_factory=lambda p: (lambda design: (_urls(design), lambda: None)),
                  architect=lambda _c, _p: new, integration_runner=_ok_runner)
    from devagent.design_diff import load_design
    assert load_design(tmp_path) == new


def test_removed_service_is_reaped(tmp_path):
    extra = ServiceNode(id="jobs", name="jobs", kind="backend", stack="node-express",
                        prd_slice="background jobs")
    change = _setup(tmp_path, prior=_design(services_extra=(extra,)))
    reaped = {}
    update_system(tmp_path, str(change), budget=None, ledger=None,
                  run_node=lambda n, d, repair_context=None: NodeResult(n.id, SUCCEEDED),
                  bring_up_factory=lambda p: (lambda design: (_urls(design), lambda: None)),
                  architect=lambda _c, _p: _design(),          # new design DROPS `jobs`
                  integration_runner=_ok_runner,
                  reap=lambda rd, prior, new: reaped.update(
                      prior={s.id for s in prior.services}, new={s.id for s in new.services}))
    assert "jobs" in reaped["prior"] and "jobs" not in reaped["new"]


def test_duplicate_names_in_new_design_fail_before_building(tmp_path):
    change = _setup(tmp_path)
    dup = _design(services_extra=(
        ServiceNode(id="web2", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="dup"),))
    called = {"run_node": False}
    def run_node(n, d, repair_context=None):
        called["run_node"] = True
        return NodeResult(n.id, SUCCEEDED)
    rep = update_system(tmp_path, str(change), budget=None, ledger=None, run_node=run_node,
                        bring_up_factory=lambda p: (lambda design: ({}, lambda: None)),
                        architect=lambda _c, _p: dup, integration_runner=_ok_runner)
    assert rep.status == "design_failed" and not called["run_node"]
