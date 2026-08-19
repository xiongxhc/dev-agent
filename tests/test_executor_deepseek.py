"""Unit tests for DeepSeekExecutor — the docker run is mocked, so no container / no tokens."""

import json

import pytest

from devagent import executor_sdk
from devagent.executor import BuildRequest
from devagent.executor_deepseek import ANTHROPIC_COMPAT_URL, DeepSeekExecutor
from devagent.schema import AcceptanceCheck, ArtifactSpec, Plan, ProjectScope, Task


def _req(workdir):
    scope = ProjectScope(
        title="Hello",
        targets=[ArtifactSpec(
            type="frontend", stack="node-vite-react", name="web", detail={},
            acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")],
        )],
    )
    plan = Plan(tasks=[Task(id="a", description="scaffold", owned_files=["package.json"])])
    return BuildRequest(scope=scope, plan=plan, workdir=str(workdir), run_id="r1")


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def _fake_ok(out, result=None):
    def fake_run(argv, **kw):
        fake_run.argv = argv
        fake_run.env = kw.get("env")
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        dev.joinpath("result.json").write_text(json.dumps(result or {"ok_stream": True}))
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc()
    return fake_run


def test_base_url_and_default_model_in_argv(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_ok(out)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-secret")
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    DeepSeekExecutor().build(_req(out))
    assert f"ANTHROPIC_BASE_URL={ANTHROPIC_COMPAT_URL}" in fake.argv
    assert "DEVAGENT_BUILD_MODEL=deepseek-v4-pro" in fake.argv


def test_key_mapped_into_env_never_in_argv(tmp_path, monkeypatch):
    """The DeepSeek key rides the existing by-name channel: the container gets
    ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN via the subprocess env, never argv."""
    out = tmp_path / "out"
    fake = _fake_ok(out)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-secret")
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    DeepSeekExecutor().build(_req(out))
    assert fake.env["ANTHROPIC_API_KEY"] == "sk-ds-secret"
    assert fake.env["ANTHROPIC_AUTH_TOKEN"] == "sk-ds-secret"
    assert "-e" in fake.argv and "ANTHROPIC_AUTH_TOKEN" in fake.argv  # passed by NAME
    assert not any("sk-ds-secret" in a for a in fake.argv)


def test_missing_key_fails_before_docker(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(executor_sdk.subprocess, "run", _fake_ok(out))
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DeepSeekExecutor().build(_req(out))


def test_cost_recomputed_at_deepseek_rates(tmp_path, monkeypatch):
    """The Agent SDK's cost_usd is Claude-priced (~10-30x too high for DeepSeek) — the arm
    must recompute from the usage breakdown, else the max_cost_usd ceiling misfires."""
    out = tmp_path / "out"
    fake = _fake_ok(out, result={
        "ok_stream": True, "tokens_in": 3_000_000, "tokens_out": 1_000_000,
        "cost_usd": 42.0,  # Claude-priced by the SDK — must NOT be trusted
        "usage": {"input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
                  "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000},
    })
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-secret")
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    res = DeepSeekExecutor().build(_req(out))
    # v4-pro: (1M miss + 1M cache-write) * $0.435 + 1M hit * $0.003625 + 1M out * $0.87
    assert res.cost_usd == pytest.approx(1.743625)


def test_cost_none_when_no_usage_breakdown(tmp_path, monkeypatch):
    """No usage → no honest DeepSeek price; report None rather than the Claude-priced figure.
    (None disables the $ ceiling — honest-unknown beats wrong-by-30x.)"""
    out = tmp_path / "out"
    fake = _fake_ok(out, result={"ok_stream": True, "cost_usd": 42.0})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-secret")
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    res = DeepSeekExecutor().build(_req(out))
    assert res.cost_usd is None


def test_unknown_model_rejected_at_construction():
    with pytest.raises(ValueError, match="deepseek-v9"):
        DeepSeekExecutor(model="deepseek-v9")
