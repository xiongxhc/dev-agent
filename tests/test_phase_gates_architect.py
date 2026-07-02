from devagent.phases.base import PhaseResult
from devagent.phase_gates import ArchitectGate
from devagent.schema import Contract, ServiceNode, SystemDesign


def _result(artifact, exit_code=0):
    return PhaseResult(name="architect", exit_code=exit_code, output_artifact=artifact)


def _good_design():
    return SystemDesign(
        title="Todo system",
        services=[
            ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                        prd_slice="A JSON API.", provides=["api.openapi"]),
            ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                        prd_slice="A UI.", depends_on=["api"], consumes=["api.openapi"]),
        ],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api")],
    )


def test_passes_for_a_valid_design():
    assert ArchitectGate().check(_result(_good_design())).ok


def test_fails_when_phase_errored():
    r = ArchitectGate().check(_result(None, exit_code=1))
    assert not r.ok


def test_fails_on_wrong_artifact_type():
    r = ArchitectGate().check(_result(object()))
    assert not r.ok


def test_gate_name_is_stable():
    assert ArchitectGate().name == "system_design_buildable"


def test_fails_on_dependency_cycle():
    # model_construct bypasses schema validation to simulate a validator miss;
    # the gate must catch the cycle and fail cleanly, not crash.
    a = ServiceNode(id="a", name="a", kind="backend", stack="node-express",
                    prd_slice="x", depends_on=["b"])
    b = ServiceNode(id="b", name="b", kind="backend", stack="node-express",
                    prd_slice="y", depends_on=["a"])
    design = SystemDesign.model_construct(title="t", services=[a, b], contracts=[], version=1)
    r = ArchitectGate().check(_result(design))
    assert not r.ok and "cycle" in r.reason.lower()


def test_fails_when_consumed_contract_not_provided_by_a_dependency():
    web = ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                      prd_slice="x", depends_on=["api"], consumes=["api.openapi"])
    api = ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                      prd_slice="y")  # provides nothing
    design = SystemDesign.model_construct(title="t", services=[web, api], contracts=[], version=1)
    r = ArchitectGate().check(_result(design))
    assert not r.ok


def test_fails_on_unresolved_dependency():
    web = ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                      prd_slice="x", depends_on=["ghost"])
    design = SystemDesign.model_construct(title="t", services=[web], contracts=[], version=1)
    r = ArchitectGate().check(_result(design))
    assert not r.ok and "ghost" in r.reason


def test_fails_on_unregistered_stack():
    svc = ServiceNode(id="api", name="api", kind="backend", stack="totally-fake-stack",
                      prd_slice="x")
    design = SystemDesign(title="t", services=[svc])
    r = ArchitectGate().check(_result(design))
    assert not r.ok and "recipe" in r.reason.lower()


def test_fails_on_kind_recipe_type_mismatch():
    # node-express is a backend recipe; declaring kind=frontend must fail
    svc = ServiceNode(id="web", name="web", kind="frontend", stack="node-express",
                      prd_slice="x")
    r = ArchitectGate().check(_result(SystemDesign(title="t", services=[svc])))
    assert not r.ok


def test_fails_on_duplicate_service_ids():
    a = ServiceNode(id="api", name="api1", kind="backend", stack="node-express", prd_slice="x")
    b = ServiceNode(id="api", name="api2", kind="backend", stack="node-express", prd_slice="y")
    design = SystemDesign.model_construct(title="t", services=[a, b], contracts=[], version=1)
    r = ArchitectGate().check(_result(design))
    assert not r.ok and "duplicate" in r.reason.lower()
