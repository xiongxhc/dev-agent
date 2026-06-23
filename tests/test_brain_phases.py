"""Brain phases (intake/spec/plan) — no live LLM calls; generate_structured is mocked.

Each phase imports generate_structured into its own module namespace, so we patch the
name there (devagent.phases.<phase>.generate_structured)."""

import pytest

from devagent.phases.base import PhaseContext
from devagent.phases.intake import IntakePhase
from devagent.phases.plan import PlanPhase
from devagent.phases.spec import SpecPhase
from devagent.schema import AcceptanceCheck, Brief, Plan, Spec, Task


class FakeSandbox:
    """Brain phases never touch the sandbox; this just satisfies PhaseContext."""

    def run(self, cmd):
        raise AssertionError("brain phases must not touch the sandbox")


def make_ctx(**artifacts) -> PhaseContext:
    return PhaseContext(
        sandbox=FakeSandbox(), budget=None, ledger=None, artifacts=dict(artifacts)
    )


BRIEF = Brief(source="prd", title="Todo App", summary="A todo list", requirements=["add"])
SPEC = Spec(
    title="Todo App",
    pages=["/"],
    components=["TodoList"],
    acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")],
)
PLAN = Plan(tasks=[Task(id="t1", description="scaffold", owned_files=["src/App.tsx"])])
USAGE = {"tokens_in": 12, "tokens_out": 3}


def test_intake_reads_file_and_returns_brief(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"
    prd.write_text("Build a todo app.")
    seen = {}

    def fake(prompt, schema, **kw):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return BRIEF, USAGE

    monkeypatch.setattr("devagent.phases.intake.generate_structured", fake)
    result = IntakePhase(str(prd)).run(make_ctx())

    assert result.exit_code == 0
    assert result.name == "intake"
    assert result.output_artifact is BRIEF
    assert result.output == BRIEF.title
    assert result.meta == USAGE
    assert seen["schema"] is Brief
    assert "Build a todo app." in seen["prompt"]


def test_intake_returns_exit_1_on_error_not_exception(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"
    prd.write_text("anything")

    def boom(prompt, schema, **kw):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr("devagent.phases.intake.generate_structured", boom)
    result = IntakePhase(str(prd)).run(make_ctx())

    assert result.exit_code == 1
    assert "llm exploded" in result.output
    assert result.output_artifact is None


def test_intake_missing_file_is_exit_1():
    result = IntakePhase("/no/such/prd.md").run(make_ctx())
    assert result.exit_code == 1


def test_spec_reads_intake_artifact(monkeypatch):
    seen = {}

    def fake(prompt, schema, **kw):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return SPEC, USAGE

    monkeypatch.setattr("devagent.phases.spec.generate_structured", fake)
    result = SpecPhase().run(make_ctx(intake=BRIEF))

    assert result.exit_code == 0
    assert result.output_artifact is SPEC
    assert result.meta == USAGE
    assert seen["schema"] is Spec
    assert BRIEF.title in seen["prompt"]  # brief is threaded into the prompt


def test_spec_missing_intake_artifact_is_exit_1(monkeypatch):
    # No "intake" artifact -> KeyError -> caught -> exit 1, not an exception.
    monkeypatch.setattr(
        "devagent.phases.spec.generate_structured",
        lambda *a, **k: (SPEC, USAGE),
    )
    result = SpecPhase().run(make_ctx())
    assert result.exit_code == 1


def test_plan_reads_spec_artifact(monkeypatch):
    seen = {}

    def fake(prompt, schema, **kw):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return PLAN, USAGE

    monkeypatch.setattr("devagent.phases.plan.generate_structured", fake)
    result = PlanPhase().run(make_ctx(spec=SPEC))

    assert result.exit_code == 0
    assert result.output_artifact is PLAN
    assert result.meta == USAGE
    assert seen["schema"] is Plan
    assert SPEC.title in seen["prompt"]  # spec is threaded into the prompt


def test_plan_missing_spec_artifact_is_exit_1(monkeypatch):
    monkeypatch.setattr(
        "devagent.phases.plan.generate_structured",
        lambda *a, **k: (PLAN, USAGE),
    )
    result = PlanPhase().run(make_ctx())
    assert result.exit_code == 1
