from devagent.phase_gates import ScopeGate
from devagent.phases.base import PhaseResult
from devagent.schema import AcceptanceCheck, ArtifactSpec, ProjectScope


def _scope(**kw):
    base = dict(
        title="t",
        targets=[ArtifactSpec(type="backend", stack="node-express", name="api",
                              acceptance_checks=[AcceptanceCheck(kind="api_json", route="/health",
                                                                 json_path="ok")])],
    )
    base.update(kw)
    return ProjectScope(**base)


def _res(scope, exit_code=0):
    return PhaseResult(name="scope", exit_code=exit_code, output_artifact=scope)


def test_valid_scope_passes():
    assert ScopeGate().check(_res(_scope())).ok


def test_pending_clarifications_fail():
    s = _scope(clarifications=["which auth?"])
    g = ScopeGate().check(_res(s))
    assert not g.ok and "clarif" in g.reason.lower()


def test_unregistered_stack_fails_no_recipe():
    s = _scope(targets=[ArtifactSpec(type="backend", stack="java-springboot", name="api",
                                     acceptance_checks=[AcceptanceCheck(kind="api_json", route="/h",
                                                                        json_path="ok")])])
    g = ScopeGate().check(_res(s))
    assert not g.ok and "no recipe" in g.reason.lower()


def test_type_mismatch_fails():
    s = _scope(targets=[ArtifactSpec(type="frontend", stack="node-express", name="api",
                                     acceptance_checks=[AcceptanceCheck(kind="api_json", route="/h",
                                                                        json_path="ok")])])
    assert not ScopeGate().check(_res(s)).ok


def test_unsupported_check_kind_fails():
    s = _scope(targets=[ArtifactSpec(type="backend", stack="node-express", name="api",
                                     acceptance_checks=[AcceptanceCheck(kind="selector_present",
                                                                        route="/h", selector="h1")])])
    assert not ScopeGate().check(_res(s)).ok


def test_phase_error_fails():
    assert not ScopeGate().check(_res(_scope(), exit_code=1)).ok
