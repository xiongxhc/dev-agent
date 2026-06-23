from devagent.phase_gates import BriefGate, PlanGate, SpecGate
from devagent.phases.base import PhaseResult
from devagent.schema import AcceptanceCheck, Brief, Plan, Spec, Task


def _result(artifact, exit_code=0):
    return PhaseResult("phase", exit_code=exit_code, output_artifact=artifact)


# --- BriefGate ---


def test_brief_gate_passes():
    brief = Brief(source="prd", title="T", summary="does a thing", requirements=["r1"])
    gr = BriefGate().check(_result(brief))
    assert gr.ok
    assert gr.reason == ""


def test_brief_gate_fails_on_no_requirements():
    brief = Brief(source="prd", title="T", summary="does a thing", requirements=[])
    gr = BriefGate().check(_result(brief))
    assert not gr.ok
    assert "no requirements" in gr.reason


# --- SpecGate ---


def _spec(**overrides):
    base = dict(
        title="T",
        pages=["/", "/about"],
        acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")],
    )
    base.update(overrides)
    return Spec(**base)


def test_spec_gate_passes():
    gr = SpecGate().check(_result(_spec()))
    assert gr.ok


def test_spec_gate_fails_when_check_route_not_in_pages():
    spec = _spec(
        acceptance_checks=[AcceptanceCheck(kind="route_status", route="/missing")],
    )
    gr = SpecGate().check(_result(spec))
    assert not gr.ok
    assert "/missing" in gr.reason


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


# --- shared failure modes, all gates ---


def test_gates_fail_on_nonzero_exit():
    for gate in (BriefGate(), SpecGate(), PlanGate()):
        gr = gate.check(PhaseResult("phase", exit_code=1, output="boom"))
        assert not gr.ok
        assert "exited 1" in gr.reason


def test_gates_fail_on_none_artifact():
    for gate in (BriefGate(), SpecGate(), PlanGate()):
        gr = gate.check(_result(None))
        assert not gr.ok
        assert "no output_artifact" in gr.reason


def test_gates_fail_on_wrong_artifact_type():
    # Feed each gate the wrong artifact type.
    brief = Brief(source="prd", title="T", summary="s", requirements=["r"])
    assert not SpecGate().check(_result(brief)).ok
    assert not PlanGate().check(_result(brief)).ok
    assert not BriefGate().check(_result(_plan())).ok
