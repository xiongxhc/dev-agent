from devagent.phase_gates import PlanGate
from devagent.phases.base import PhaseResult
from devagent.schema import ArtifactSpec, Plan, ProjectScope, Task


def _result(artifact, exit_code=0):
    return PhaseResult("phase", exit_code=exit_code, output_artifact=artifact)


# --- PlanGate ---


def _plan():
    return Plan(
        tasks=[
            Task(id="t1", description="a", owned_files=["src/A.tsx"]),
            Task(id="t2", description="b", owned_files=["src/B.tsx"]),
        ]
    )


def test_plan_gate_passes():
    gr = PlanGate().check(_result(_plan()))
    assert gr.ok


def test_plan_gate_catches_overlap_via_model_construct():
    # The Plan validator rejects overlapping owned_files at construction, so build an
    # invalid Plan bypassing validation to exercise the gate's defense-in-depth check.
    plan = Plan.model_construct(
        tasks=[
            Task(id="t1", description="a", owned_files=["src/Shared.tsx"]),
            Task(id="t2", description="b", owned_files=["src/Shared.tsx"]),
        ]
    )
    gr = PlanGate().check(_result(plan))
    assert not gr.ok
    assert "src/Shared.tsx" in gr.reason
    assert "t1" in gr.reason and "t2" in gr.reason


# --- shared failure modes ---


def test_plan_gate_fails_on_nonzero_exit():
    gr = PlanGate().check(PhaseResult("phase", exit_code=1, output="boom"))
    assert not gr.ok
    assert "exited 1" in gr.reason


def test_plan_gate_fails_on_none_artifact():
    gr = PlanGate().check(_result(None))
    assert not gr.ok
    assert "no output_artifact" in gr.reason


def test_plan_gate_fails_on_wrong_artifact_type():
    wrong = ProjectScope(title="Hello", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                     detail={"pages": ["/"]})
    ])
    gr = PlanGate().check(_result(wrong))
    assert gr.ok is False
