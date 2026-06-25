"""Brain phases (plan) — no live LLM calls; generate_structured is mocked.

Each phase imports generate_structured into its own module namespace, so we patch the
name there (devagent.phases.<phase>.generate_structured).

Intake and spec phases have been retired in M6; their tests were removed here.
Scope phase tests are in test_scope_phase.py."""

from devagent.phases.base import PhaseContext
from devagent.phases.plan import PlanPhase
from devagent.schema import AcceptanceCheck, ArtifactSpec, Plan, ProjectScope, Task


class FakeSandbox:
    """Brain phases never touch the sandbox; this just satisfies PhaseContext."""

    def run(self, cmd):
        raise AssertionError("brain phases must not touch the sandbox")


def make_ctx(**artifacts) -> PhaseContext:
    return PhaseContext(
        sandbox=FakeSandbox(), budget=None, ledger=None, artifacts=dict(artifacts)
    )


PLAN = Plan(tasks=[Task(id="t1", description="scaffold", owned_files=["web/src/App.tsx"])])
SCOPE = ProjectScope(title="Todo App", targets=[
    ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                 detail={"pages": ["/"]},
                 acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")]),
])
USAGE = {"tokens_in": 12, "tokens_out": 3}


def test_plan_reads_scope_artifact(monkeypatch):
    seen = {}

    def fake(prompt, schema, **kw):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return PLAN, USAGE

    monkeypatch.setattr("devagent.phases.plan.generate_structured", fake)
    result = PlanPhase().run(make_ctx(scope=SCOPE))

    assert result.exit_code == 0
    assert result.output_artifact is PLAN
    assert result.meta == USAGE
    assert seen["schema"] is Plan
    assert SCOPE.title in seen["prompt"]  # scope title is threaded into the prompt


def test_plan_missing_scope_artifact_is_exit_1(monkeypatch):
    monkeypatch.setattr(
        "devagent.phases.plan.generate_structured",
        lambda *a, **k: (PLAN, USAGE),
    )
    result = PlanPhase().run(make_ctx())
    assert result.exit_code == 1
