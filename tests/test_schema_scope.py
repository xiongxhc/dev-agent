import pytest
from pydantic import ValidationError
from devagent.schema import AcceptanceCheck, ArtifactSpec, AuthFlow, ProjectScope, RepoBinding


def test_auth_check_requires_an_auth_flow_on_the_target():
    # a check marked auth=True but no `auth` flow declared -> invalid (runner would have no token)
    with pytest.raises(ValidationError):
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     acceptance_checks=[AcceptanceCheck(kind="api_json", route="/todos", auth=True)])
    # with an auth flow declared, it validates
    spec = ArtifactSpec(
        type="backend", stack="node-express", name="api",
        auth=AuthFlow(login_route="/auth/login", token_json_path="token",
                      login_body={"username": "u", "password": "p"}),
        acceptance_checks=[AcceptanceCheck(kind="api_json", route="/todos", auth=True)],
    )
    assert spec.auth.scheme == "Bearer" and spec.acceptance_checks[0].auth is True


def test_authflow_routes_must_be_paths():
    with pytest.raises(ValidationError):
        AuthFlow(login_route="auth/login", token_json_path="token")  # missing leading /


def test_acceptance_check_auth_defaults_false():
    assert AcceptanceCheck(kind="route_status", route="/").auth is False


def test_api_json_check_requires_route():
    with pytest.raises(ValidationError):
        AcceptanceCheck(kind="api_json")  # no route
    c = AcceptanceCheck(kind="api_json", route="/api/items", json_path="data.0.id")
    assert c.method == "GET" and c.expected_status == 200


def test_command_exit_requires_argv():
    with pytest.raises(ValidationError):
        AcceptanceCheck(kind="command_exit")  # no argv
    c = AcceptanceCheck(kind="command_exit", argv=["node", "dist/cli.js", "--version"])
    assert c.expected_exit == 0


def test_stdout_matches_requires_pattern_and_argv():
    with pytest.raises(ValidationError):
        AcceptanceCheck(kind="stdout_matches", argv=["x"])  # no pattern
    AcceptanceCheck(kind="stdout_matches", argv=["x"], pattern="^ok$")


def test_projectscope_needs_a_target():
    with pytest.raises(ValidationError):
        ProjectScope(title="t", targets=[])
    s = ProjectScope(
        title="Todo",
        targets=[
            ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                         detail={"pages": ["/"]},
                         acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")]),
            ArtifactSpec(type="backend", stack="node-express", name="api",
                         detail={"endpoints": ["/api/todos"]},
                         acceptance_checks=[AcceptanceCheck(kind="api_json", route="/api/todos",
                                                            json_path="0.id")]),
        ],
    )
    assert s.repo is None and s.clarifications == []
    assert [t.type for t in s.targets] == ["frontend", "backend"]


def test_repo_binding_default_none_mode():
    assert RepoBinding().mode == "none"
