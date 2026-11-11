"""The map stage's contained session, with Docker and the model faked.

Mapping sends a repo's source code to a model API and asks a model with tools to describe it.
So: the session can read but not change the code, the endpoint is Claude unless the operator
names another one, no credential value ever reaches the `docker run` argv, and an answer that
does not validate is asked again rather than repaired.
"""

import json
from pathlib import Path

import pytest

from devagent.adopt import mapper, mapper_runner
from devagent.adopt.mapper import adopt_arm
from devagent.adopt.survey import ServiceRoot
from devagent.executor_deepseek import ANTHROPIC_COMPAT_URL, DEFAULT_MODEL


@pytest.fixture(autouse=True)
def _no_arm_from_the_host(monkeypatch):
    monkeypatch.delenv("DEVAGENT_ADOPT_ARM", raising=False)


def _svc(path="api"):
    return ServiceRoot(name="api", path=path, kind="maven-module", deployable=True)


def test_the_mapper_arm_is_claude_unless_deepseek_is_named():
    assert adopt_arm({}) == "claude"
    assert adopt_arm({"DEVAGENT_ADOPT_ARM": ""}) == "claude"
    assert adopt_arm({"DEVAGENT_ADOPT_ARM": "claude"}) == "claude"
    assert adopt_arm({"DEVAGENT_ADOPT_ARM": "deepseek"}) == "deepseek"
    with pytest.raises(ValueError, match="known: claude, deepseek"):
        adopt_arm({"DEVAGENT_ADOPT_ARM": "gpt"})


def test_the_old_backend_variable_does_not_choose_the_arm():
    assert adopt_arm({"DEVAGENT_ADOPT_BACKEND": "deepseek"}) == "claude"


def test_the_mapping_session_is_read_only():
    """Two independent locks: the mount is :ro AND the tool set has no writing tool."""
    argv = mapper._container_argv("img", Path("/repo"), "api", None, None)
    assert "-v" in argv and "/repo:/src:ro" in argv
    runner = mapper.RUNNER.read_text()
    assert 'allowed_tools=["Read", "Grep", "Glob"]' in runner
    for forbidden in ("Write", "Edit", "Bash"):
        assert f'"{forbidden}"' not in runner.split("allowed_tools")[1][:120], forbidden


def test_no_credential_value_reaches_the_docker_argv(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-claude")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-deepseek")
    for arm in ("claude", "deepseek"):
        monkeypatch.setenv("DEVAGENT_ADOPT_ARM", arm)
        argv = mapper._container_argv("img", Path("/repo"), "api", None, None)
        joined = " ".join(argv)
        assert "sk-secret" not in joined, arm
        assert "ANTHROPIC_API_KEY" in argv, "the key is passed by name"


def test_the_deepseek_arm_points_the_session_at_deepseek(monkeypatch):
    monkeypatch.setenv("DEVAGENT_ADOPT_ARM", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-deepseek")
    argv = mapper._container_argv("img", Path("/repo"), "api", None, None)
    assert f"ANTHROPIC_BASE_URL={ANTHROPIC_COMPAT_URL}" in argv
    assert f"ANTHROPIC_MODEL={DEFAULT_MODEL}" in argv
    assert "ANTHROPIC_AUTH_TOKEN" in argv, "the token is passed by name"
    env = mapper._subprocess_env()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret-deepseek"
    assert env["ANTHROPIC_API_KEY"] == "sk-secret-deepseek"


def test_the_claude_arm_sets_no_base_url(monkeypatch):
    monkeypatch.delenv("DEVAGENT_ADOPT_ARM", raising=False)
    argv = mapper._container_argv("img", Path("/repo"), "api", None, None)
    assert not any(a.startswith("ANTHROPIC_BASE_URL=") for a in argv)
    assert not any(a.startswith("ANTHROPIC_MODEL=") for a in argv)


def test_the_session_runs_behind_the_egress_proxy_when_given_one():
    argv = mapper._container_argv("img", Path("/repo"), "api", "egress-net",
                                  "http://proxy:3128")
    assert argv[argv.index("--network") + 1] == "egress-net"
    assert "HTTPS_PROXY=http://proxy:3128" in argv


def test_a_missing_credential_refuses_to_run(monkeypatch):
    monkeypatch.delenv("DEVAGENT_ADOPT_ARM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        mapper._subprocess_env()


def test_a_missing_deepseek_credential_refuses_to_run(monkeypatch):
    monkeypatch.setenv("DEVAGENT_ADOPT_ARM", "deepseek")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-claude")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY") as e:
        mapper._subprocess_env()
    assert "sk-secret" not in str(e.value)


def test_an_invalid_fragment_is_re_asked_then_given_up_on(tmp_path):
    """A fragment that fails validation is re-asked, never hand-repaired: a repaired answer is
    no longer what the model read."""
    calls = []

    def bad(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return '{"summary": "too short"}'          # fails min_length

    assert mapper.map_service(tmp_path, _svc(), runner=bad) is None
    assert len(calls) == 3, "one attempt plus two retries"
    assert calls[1] and "summary" in calls[1], "the validator's message is fed back"


def test_a_contract_kind_the_schema_refuses_is_re_asked_not_accepted(tmp_path):
    calls = []

    def grpc(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return json.dumps({"summary": "A service that exposes a gRPC surface only.",
                           "provides": [{"id": "api", "kind": "grpc", "spec": {}}]})

    assert mapper.map_service(tmp_path, _svc(), runner=grpc) is None
    assert len(calls) == 3 and "kind" in calls[1]


def test_a_valid_fragment_becomes_a_fragment(tmp_path):
    seen = []

    def good(image, repo, path, network, proxy, timeout, prior):
        seen.append(path)
        return json.dumps({
            "summary": "An HTTP API that stores orders and reads prices from a sibling service.",
            "kind": "backend",
            "provides": [{"id": "api-api", "kind": "openapi",
                          "spec": {"paths": {"/orders": {"get": {}}}}}],
            "consumes": ["prices-api"],
            "depends_on": ["prices"]})

    frag = mapper.map_service(tmp_path, _svc(), runner=good)
    assert seen == ["api"], "the runner is given the service's path"
    assert frag.service == "api"
    assert frag.prd_slice.startswith("An HTTP API that stores orders")
    assert frag.kind == "backend"
    assert frag.provides == ({"id": "api-api", "kind": "openapi",
                              "spec": {"paths": {"/orders": {"get": {}}}}},)
    assert frag.consumes == ("prices-api",)
    assert frag.depends_on == ("prices",)


@pytest.mark.parametrize("path", ["/etc", "../other", "api/../../other", "api/.."])
def test_a_path_outside_the_repo_is_not_mapped_and_nothing_runs(tmp_path, path):
    def never(*a):
        pytest.fail("no session may start for a path outside the repo")

    assert mapper.map_service(tmp_path, _svc(path), runner=never) is None


def test_the_repo_root_is_a_mappable_path(tmp_path):
    seen = []

    def empty(image, repo, path, network, proxy, timeout, prior):
        seen.append(path)
        return None

    assert mapper.map_service(tmp_path, _svc("."), runner=empty) is None
    assert seen == ["."]


def test_braces_in_a_service_path_do_not_change_the_prompt():
    odd = "we{b}/{retry}/{0}"
    plain = mapper_runner.build_prompt("X", None)
    assert mapper_runner.build_prompt(odd, None) == plain.replace("/src/X", f"/src/{odd}")


def test_a_retry_carries_the_validation_error_into_the_prompt():
    prompt = mapper_runner.build_prompt("api", "summary: too short {x}")
    assert "summary: too short {x}" in prompt
    assert "/src/api" in prompt
