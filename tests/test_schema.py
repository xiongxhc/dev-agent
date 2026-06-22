import pytest
from pydantic import ValidationError

from devagent.schema import AcceptanceCheck, Brief, Plan, Spec, Task


def _check(**kw):
    base = dict(kind="route_status", route="/", expected_status=200)
    base.update(kw)
    return AcceptanceCheck(**base)


def test_spec_requires_at_least_one_acceptance_check():
    with pytest.raises(ValidationError):
        Spec(title="t", pages=["/"], acceptance_checks=[])


def test_acceptance_check_route_must_be_a_path():
    with pytest.raises(ValidationError):
        _check(route="not-a-path")


def test_selector_check_requires_a_selector():
    with pytest.raises(ValidationError):
        _check(kind="selector_present", route="/", selector=None)
    ok = _check(kind="selector_present", route="/", selector="h1")
    assert ok.selector == "h1"


def test_valid_spec_round_trips():
    spec = Spec(
        title="Hello",
        pages=["/"],
        acceptance_checks=[_check(), _check(kind="selector_present", selector="h1")],
    )
    assert spec.stack == "vite-react-tailwind"
    assert len(spec.acceptance_checks) == 2


def test_plan_rejects_overlapping_file_ownership():
    with pytest.raises(ValidationError):
        Plan(tasks=[
            Task(id="a", description="pages", owned_files=["src/App.tsx"]),
            Task(id="b", description="styles", owned_files=["src/App.tsx"]),  # collision
        ])


def test_plan_accepts_disjoint_ownership():
    plan = Plan(tasks=[
        Task(id="a", description="pages", owned_files=["src/App.tsx"]),
        Task(id="b", description="styles", owned_files=["src/index.css"]),
    ])
    assert len(plan.tasks) == 2


def test_brief_requires_title_and_summary():
    with pytest.raises(ValidationError):
        Brief(source="prd", title="", summary="x")
    b = Brief(source="prd", title="T", summary="S", requirements=["r1"])
    assert b.requirements == ["r1"]
