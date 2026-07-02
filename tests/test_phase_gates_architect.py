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
