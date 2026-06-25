import pytest
from pydantic import ValidationError

from devagent.schema import AcceptanceCheck, ArtifactSpec, Plan, ProjectScope, Task


def _check(**kw):
    base = dict(kind="route_status", route="/", expected_status=200)
    base.update(kw)
    return AcceptanceCheck(**base)


def test_acceptance_check_route_must_be_a_path():
    with pytest.raises(ValidationError):
        _check(route="not-a-path")


def test_selector_check_requires_a_selector():
    with pytest.raises(ValidationError):
        _check(kind="selector_present", route="/", selector=None)
    ok = _check(kind="selector_present", route="/", selector="h1")
    assert ok.selector == "h1"


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


def test_project_scope_requires_at_least_one_target():
    with pytest.raises(ValidationError):
        ProjectScope(title="T", targets=[])


def test_project_scope_round_trips():
    scope = ProjectScope(
        title="Hello",
        targets=[
            ArtifactSpec(
                type="backend", stack="node-express", name="api",
                detail={"endpoints": ["/health"]},
                acceptance_checks=[AcceptanceCheck(kind="api_json", route="/health", json_path="ok")],
            )
        ],
    )
    assert scope.title == "Hello"
    assert len(scope.targets) == 1
    assert scope.clarifications == []
