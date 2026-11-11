"""The map stage's contained session, with Docker and the model faked.

Mapping sends a repo's source code to a model API and asks a model with tools to describe it.
So: the session is locked to reading inside the repo, the endpoint is Claude unless the
operator names another one, no credential value reaches the `docker run` argv or the answer
that is kept, and an answer that does not validate is asked again rather than repaired.
"""

import asyncio
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


def test_the_repo_is_mounted_read_only():
    argv = mapper._container_argv("img", Path("/repo"), "api", None, None)
    assert "-v" in argv and "/repo:/src:ro" in argv


def test_the_session_options_restrict_tools_rather_than_pre_approve_them():
    opts = mapper_runner.session_options()
    assert opts["permission_mode"] == "dontAsk"
    assert "bypassPermissions" not in repr(opts)
    assert opts["allowed_tools"] == ["Read", "Grep", "Glob"]
    for tool in ("Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent"):
        assert tool in opts["disallowed_tools"], tool
    for tool in ("Read", "Grep", "Glob"):
        assert tool not in opts["disallowed_tools"], tool
    assert opts["setting_sources"] == []
    assert opts["cwd"] == "/src"
    assert opts["system_prompt"] == mapper_runner.SYSTEM


def test_the_runner_never_uses_bypass_permissions():
    assert "bypassPermissions" not in mapper.RUNNER.read_text()
    assert "bypassPermissions" not in Path(mapper.__file__).read_text()


@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "NotebookEdit", "Agent", "WebFetch",
                                  "SomeFutureTool", "mcp__x__y", "read", ""])
def test_the_guard_denies_every_tool_but_the_three_reading_tools(tool):
    assert mapper_runner.decide(tool, {"file_path": "/src/app/main.py"}) is not None


def test_the_guard_denies_a_tool_name_that_is_not_a_string():
    assert mapper_runner.decide(None, {"file_path": "/src/app/main.py"}) is not None


def test_the_guard_allows_reading_inside_the_repo():
    assert mapper_runner.decide("Read", {"file_path": "/src/app/main.py"}) is None
    assert mapper_runner.decide("Read", {"file_path": "app/main.py"}) is None, "relative to /src"
    assert mapper_runner.decide("Read", {"file_path": "/src"}) is None


@pytest.mark.parametrize("path", ["/etc/passwd", "/proc/self/environ", "/src/../etc/passwd",
                                  "/srcx/a", "../etc/passwd", "//host/share/a", "~/x",
                                  "/src/a\x00b"])
def test_the_guard_denies_reading_outside_the_repo(path):
    assert mapper_runner.decide("Read", {"file_path": path}) is not None


def test_a_symlink_is_judged_by_where_it_points(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "pkg").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (root / "pkg" / "real.py").write_text("x")
    (root / "out-link").symlink_to(outside / "secret.txt")
    (root / "out-dir").symlink_to(outside)
    (root / "in-link").symlink_to(root / "pkg" / "real.py")
    r = str(root)
    assert mapper_runner.decide("Read", {"file_path": f"{r}/out-link"}, root=r) is not None
    assert mapper_runner.decide("Read", {"file_path": f"{r}/out-dir/secret.txt"}, root=r) \
        is not None
    assert mapper_runner.decide("Grep", {"pattern": "x", "path": f"{r}/out-dir"}, root=r) \
        is not None
    assert mapper_runner.decide("Read", {"file_path": f"{r}/in-link"}, root=r) is None


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_a_search_is_judged_by_its_path(tool):
    assert mapper_runner.decide(tool, {"pattern": "x"}) is None, "no path searches /src"
    assert mapper_runner.decide(tool, {"pattern": "x", "path": None}) is None
    assert mapper_runner.decide(tool, {"pattern": "x", "path": "/src/api"}) is None
    assert mapper_runner.decide(tool, {"pattern": "x", "path": "/etc"}) is not None
    assert mapper_runner.decide(tool, {"pattern": "x", "path": "/src/.."}) is not None


def test_a_search_pattern_cannot_climb_out_of_the_repo():
    assert mapper_runner.decide("Glob", {"pattern": "**/*.java"}) is None
    assert mapper_runner.decide("Glob", {"pattern": "/proc/*/environ"}) is not None
    assert mapper_runner.decide("Glob", {"pattern": "../**/*"}) is not None
    assert mapper_runner.decide("Grep", {"pattern": "x", "glob": "*.java"}) is None
    assert mapper_runner.decide("Grep", {"pattern": "x", "glob": "../../**"}) is not None


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", None), ("Read", "/src/a"), ("Read", {}), ("Read", {"file_path": None}),
    ("Read", {"file_path": 3}), ("Read", {"file_path": ""}),
    ("Grep", {"pattern": "x", "path": 3}), ("Glob", ["/src"]), ("Glob", {"pattern": 3}),
])
def test_a_malformed_tool_input_is_denied(tool, tool_input):
    assert mapper_runner.decide(tool, tool_input) is not None


def _await(coro):
    """Run a coroutine on a private loop, leaving the global event loop state alone."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_the_hook_turns_a_denial_into_the_documented_deny_output():
    deny = _await(mapper_runner.guard(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": "env"}}, "t1", None))
    out = deny["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"]
    allow = _await(mapper_runner.guard(
        {"hook_event_name": "PreToolUse", "tool_name": "Read",
         "tool_input": {"file_path": "/src/app/main.py"}}, "t2", None))
    assert allow == {}


@pytest.mark.parametrize("version,ok", [("0.2.177", True), ("0.2.200", True), ("0.3.0", True),
                                        ("1.0", True), ("0.2.176", False), ("0.1.999", False),
                                        ("0.2.177rc1", False), ("garbage", False), ("", False)])
def test_the_sdk_floor_is_the_first_release_that_blocks_on_hook_failure(version, ok):
    assert mapper_runner.sdk_supports(version) is ok


def test_an_sdk_too_old_for_the_locks_refuses_to_run(monkeypatch, capsys):
    monkeypatch.setattr(mapper_runner, "_installed_sdk_version", lambda: "0.2.100")
    monkeypatch.setattr("sys.argv", ["mapper.py", "--service=api"])
    assert mapper_runner.main() == 1
    assert "error" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_missing_sdk_refuses_to_run(monkeypatch, capsys):
    monkeypatch.setattr(mapper_runner, "_installed_sdk_version", lambda: None)
    monkeypatch.setattr("sys.argv", ["mapper.py", "--service=api"])
    assert mapper_runner.main() == 1
    assert "error" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_service_path_is_passed_as_one_option_value():
    argv = mapper._container_argv("img", Path("/repo"), "-api", None, None)
    assert argv[-1] == "--service=-api"
    assert argv[argv.index("img") + 1:] == ["python3", "/mapper.py", "--service=-api"]


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


_FAKE_KEY = "sk-fake-0123456789abcdefghijklmnopqrstuvwxyz-0123456789abcdefghijklmnop"


def _answer(**over):
    base = {"summary": "An HTTP API that stores orders and reads prices from a sibling service.",
            "kind": "backend",
            "provides": [{"id": "api-api", "kind": "openapi", "spec": {}}]}
    base.update(over)
    return json.dumps(base)


@pytest.mark.parametrize("arm,var", [("claude", "ANTHROPIC_API_KEY"),
                                     ("deepseek", "DEEPSEEK_API_KEY")])
def test_an_answer_carrying_the_credential_is_not_mapped_and_not_re_asked(
        tmp_path, monkeypatch, arm, var):
    monkeypatch.setenv("DEVAGENT_ADOPT_ARM", arm)
    monkeypatch.setenv(var, _FAKE_KEY)
    calls = []

    def leaky(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return _answer(summary=f"A service whose environment holds {_FAKE_KEY} for its calls.")

    assert mapper.map_service(tmp_path, _svc(), runner=leaky) is None
    assert len(calls) == 1, "never fed back"


def test_a_valid_answer_carrying_the_credential_in_a_spec_is_not_mapped(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)

    def leaky(*a):
        return _answer(provides=[{"id": "api-api", "kind": "openapi",
                                  "spec": {"x-token": _FAKE_KEY}}])

    assert mapper.map_service(tmp_path, _svc(), runner=leaky) is None


def test_an_escaped_credential_in_the_answer_is_still_recognised(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    escaped = "".join(f"\\u{ord(c):04x}" for c in _FAKE_KEY)
    raw = _answer(summary="A service whose environment holds KEY for its outbound calls.")
    raw = raw.replace("KEY", escaped)
    assert _FAKE_KEY not in raw and json.loads(raw)["summary"].count(_FAKE_KEY) == 1
    assert mapper.map_service(tmp_path, _svc(), runner=lambda *a: raw) is None


def test_the_retry_context_names_the_failure_without_echoing_the_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    calls = []
    long_value = "Q" * 30 + "middle-of-a-long-value" + "Z" * 30

    def bad(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return _answer(kind=long_value)

    assert mapper.map_service(tmp_path, _svc(), runner=bad) is None
    assert len(calls) == 3
    assert "kind" in calls[1]
    for prior in calls[1:]:
        assert "QQQQ" not in prior and "ZZZZ" not in prior and "middle" not in prior


def test_a_retry_context_carrying_the_credential_is_not_sent(tmp_path, monkeypatch):
    """A validator message that somehow carries the key ends the mapping instead."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    monkeypatch.setattr(mapper, "_retry_context", lambda e: f"bad: {_FAKE_KEY}")
    calls = []

    def bad(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return '{"summary": "too short"}'

    assert mapper.map_service(tmp_path, _svc(), runner=bad) is None
    assert calls == [None]


def test_a_duplicate_provided_contract_is_re_asked(tmp_path):
    calls = []

    def dup(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return _answer(provides=[{"id": "api-api", "kind": "openapi", "spec": {}},
                                 {"id": "api-api", "kind": "openapi", "spec": {}}])

    assert mapper.map_service(tmp_path, _svc(), runner=dup) is None
    assert len(calls) == 3 and "provides" in calls[1]


def test_consuming_an_own_provided_contract_is_re_asked(tmp_path):
    calls = []

    def loop(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return _answer(consumes=["api-api"])

    assert mapper.map_service(tmp_path, _svc(), runner=loop) is None
    assert len(calls) == 3 and "consumes" in calls[1]


def test_a_failed_session_is_not_re_asked(tmp_path):
    calls = []

    def failed(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return json.dumps({"error": "CLIConnectionError: endpoint unreachable"})

    assert mapper.map_service(tmp_path, _svc(), runner=failed) is None
    assert calls == [None]


@pytest.mark.parametrize("kind", ["service", "Backend", "library"])
def test_a_kind_outside_the_three_is_re_asked(tmp_path, kind):
    calls = []

    def odd(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return _answer(kind=kind)

    assert mapper.map_service(tmp_path, _svc(), runner=odd) is None
    assert len(calls) == 3 and "kind" in calls[1]


def test_no_kind_is_accepted(tmp_path):
    frag = mapper.map_service(tmp_path, _svc(), runner=lambda *a: _answer(kind=None))
    assert frag is not None and frag.kind is None


def test_an_overlong_summary_is_re_asked(tmp_path):
    calls = []

    def long(image, repo, path, network, proxy, timeout, prior):
        calls.append(prior)
        return _answer(summary="A" * 4001)

    assert mapper.map_service(tmp_path, _svc(), runner=long) is None
    assert len(calls) == 3 and "summary" in calls[1]
    assert mapper.map_service(tmp_path, _svc(), runner=lambda *a: _answer(summary="A" * 4000))
