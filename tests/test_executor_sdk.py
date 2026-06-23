"""Unit tests for SdkExecutor — the docker run is mocked, so no container / no tokens."""

import json
from pathlib import Path

from devagent import executor_sdk
from devagent.executor import BuildRequest
from devagent.executor_sdk import SdkExecutor
from devagent.schema import AcceptanceCheck, Plan, Spec, Task


def _req(workdir):
    spec = Spec(title="Hello", pages=["/"],
                acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])
    plan = Plan(tasks=[Task(id="a", description="scaffold", owned_files=["package.json"])])
    return BuildRequest(spec=spec, plan=plan, workdir=str(workdir), run_id="r1")


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def test_success_when_stream_ok_and_dist_present(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        # the executor must have written spec/plan before invoking docker
        assert (dev / "spec.json").is_file() and (dev / "plan.json").is_file()
        dev.joinpath("result.json").write_text(json.dumps(
            {"ok_stream": True, "tokens_in": 1200, "tokens_out": 800, "cost_usd": 0.05}))
        (out / "dist").mkdir(parents=True, exist_ok=True)
        (out / "dist" / "index.html").write_text("<html></html>")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    res = SdkExecutor().build(_req(out))
    assert res.success is True
    assert res.tokens_in == 1200 and res.tokens_out == 800
    assert res.cost_usd == 0.05
    assert res.error is None


def test_failure_when_no_dist_produced(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text(json.dumps({"ok_stream": True}))
        return _Proc()  # no dist/ created

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    res = SdkExecutor().build(_req(out))
    assert res.success is False
    assert "dist" in (res.error or "")


def test_repair_context_written_for_the_runner(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text("{}")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    req = _req(out)
    import dataclasses
    req = dataclasses.replace(req, repair_context="error TS2304: Cannot find name 'Foo'")
    SdkExecutor().build(req)
    repair = out / ".devagent" / "repair.txt"
    assert repair.is_file()
    assert "TS2304" in repair.read_text()


def test_no_repair_file_on_a_fresh_build(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text("{}")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    SdkExecutor().build(_req(out))  # repair_context is None
    assert not (out / ".devagent" / "repair.txt").exists()


def test_key_passed_by_name_never_in_argv(tmp_path, monkeypatch):
    out = tmp_path / "out"
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text("{}")
        return _Proc()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    SdkExecutor().build(_req(out))
    # the literal key value must NOT appear anywhere in argv
    assert "sk-secret-value" not in " ".join(captured["argv"])
    assert "ANTHROPIC_API_KEY" in captured["argv"]  # passed by name only
