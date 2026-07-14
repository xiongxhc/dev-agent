"""Unit tests for SdkExecutor — the docker run is mocked, so no container / no tokens."""

import json
from pathlib import Path

from devagent import executor_sdk
from devagent.executor import BuildRequest
from devagent.executor_sdk import SdkExecutor
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


def _fullstack_req(workdir):
    """A web ∥ api fullstack scope: two independent build targets (M12 parallel path)."""
    scope = ProjectScope(
        title="Fullstack",
        targets=[
            ArtifactSpec(type="frontend", stack="node-vite-react", name="web", detail={},
                         acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")]),
            ArtifactSpec(type="backend", stack="node-express", name="api", detail={},
                         acceptance_checks=[AcceptanceCheck(kind="route_status", route="/health")]),
        ],
    )
    plan = Plan(tasks=[
        Task(id="w", description="web", owned_files=["web/package.json", "web/src/App.tsx"]),
        Task(id="a", description="api", owned_files=["api/package.json", "api/src/server.ts"]),
    ])
    return BuildRequest(scope=scope, plan=plan, workdir=str(workdir), run_id="r1")


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def _fake_ok(out):
    def fake_run(argv, **kw):
        fake_run.argv = argv
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        dev.joinpath("result.json").write_text(json.dumps({"ok_stream": True}))
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc()
    return fake_run


def test_build_model_passed_as_env_when_set(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_ok(out)
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    SdkExecutor(model="claude-haiku-4-5-20251001").build(_req(out))
    assert "DEVAGENT_BUILD_MODEL=claude-haiku-4-5-20251001" in fake.argv


def test_build_model_env_omitted_when_unset(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_ok(out)
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    SdkExecutor().build(_req(out))  # model=None (the default)
    assert not any("DEVAGENT_BUILD_MODEL" in a for a in fake.argv)


def test_success_when_stream_ok_and_dist_present(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        # the executor must have written scope/plan before invoking docker
        assert (dev / "scope.json").is_file() and (dev / "plan.json").is_file()
        dev.joinpath("result.json").write_text(json.dumps(
            {"ok_stream": True, "tokens_in": 1200, "tokens_out": 800, "cost_usd": 0.05}))
        # artifact lives under the target subdirectory (web/dist/index.html)
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    res = SdkExecutor().build(_req(out))
    assert res.success is True
    assert res.tokens_in == 1200 and res.tokens_out == 800
    assert res.cost_usd == 0.05
    assert res.error is None


def test_cache_read_tokens_extracted_for_budget(tmp_path, monkeypatch):
    """The cheap cache-read portion is pulled from the raw usage breakdown into
    cache_read_tokens, so budget_tokens (the runaway count) excludes it."""
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        dev.joinpath("result.json").write_text(json.dumps({
            "ok_stream": True, "tokens_in": 1_388_702, "tokens_out": 30_267,
            "usage": {"input_tokens": 3312, "cache_creation_input_tokens": 58189,
                      "cache_read_input_tokens": 1_327_201, "output_tokens": 30_267},
        }))
        (out / "web" / "dist").mkdir(parents=True, exist_ok=True)
        (out / "web" / "dist" / "index.html").write_text("<html></html>")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    res = SdkExecutor().build(_req(out))
    assert res.cache_read_tokens == 1_327_201          # extracted from usage
    assert res.tokens_in == 1_388_702                  # reported total still includes it
    assert res.budget_tokens == 91_768                 # runaway count excludes cache-read


def test_failure_when_no_dist_produced(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fake_run(argv, **kw):
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text(json.dumps({"ok_stream": True}))
        return _Proc()  # no target artifact created

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    res = SdkExecutor().build(_req(out))
    assert res.success is False
    assert "target artifacts" in (res.error or "")


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


def test_egress_network_and_proxy_in_argv_when_configured(tmp_path, monkeypatch):
    out = tmp_path / "out"
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text("{}")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    SdkExecutor(network="devagent-egress", proxy_url="http://devagent-proxy:3128").build(_req(out))
    argv = captured["argv"]
    assert "--network" in argv and "devagent-egress" in argv
    assert any(a == "HTTPS_PROXY=http://devagent-proxy:3128" for a in argv)


def test_no_network_flag_when_egress_disabled(tmp_path, monkeypatch):
    out = tmp_path / "out"
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        (out / ".devagent").mkdir(parents=True, exist_ok=True)
        (out / ".devagent" / "result.json").write_text("{}")
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    SdkExecutor().build(_req(out))  # no network configured
    assert "--network" not in captured["argv"]


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


def test_update_context_writes_update_txt_not_repair_txt(tmp_path):
    from devagent.schema import ArtifactSpec, Plan, ProjectScope, Task

    class _NoDocker(SdkExecutor):
        def _run_one(self, out, target):
            return {"ok_stream": True}

    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web")])
    plan = Plan(tasks=[Task(id="t1", description="d", owned_files=["web/src/App.tsx"])])
    dev = tmp_path / ".devagent"

    _NoDocker().build(BuildRequest(scope=scope, plan=plan, workdir=str(tmp_path), run_id="r",
                                   repair_context="add dark mode", context_kind="update"))
    assert (dev / "update.txt").read_text() == "add dark mode"
    assert not (dev / "repair.txt").exists()

    # a follow-up REPAIR pass must clear the stale update context
    _NoDocker().build(BuildRequest(scope=scope, plan=plan, workdir=str(tmp_path), run_id="r",
                                   repair_context="TRACE", context_kind="repair"))
    assert (dev / "repair.txt").read_text() == "TRACE"
    assert not (dev / "update.txt").exists()


# ---------------------------------------------------------------------------
# M12 — parallel / team build: one contained SDK session per build-target.
# ---------------------------------------------------------------------------

def _fake_multi(out, per_target):
    """Fake docker run for the multi-target path: each call carries `--target NAME`, writes
    that target's result + artifact under .devagent/build/<name>/ and <name>/dist."""
    def fake_run(argv, **kw):
        tgt = argv[argv.index("--target") + 1]
        fake_run.targets_seen.append(tgt)
        bdir = out / ".devagent" / "build" / tgt
        bdir.mkdir(parents=True, exist_ok=True)
        bdir.joinpath("result.json").write_text(json.dumps(per_target[tgt]))
        artifact = {"web": "web/dist/index.html", "api": "api/dist/server.js"}[tgt]
        p = out / artifact
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return _Proc()
    fake_run.targets_seen = []
    return fake_run


def test_multi_target_dispatches_one_session_per_build_target(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_multi(out, {
        "web": {"ok_stream": True, "tokens_in": 1000, "tokens_out": 500, "cost_usd": 0.03},
        "api": {"ok_stream": True, "tokens_in": 2000, "tokens_out": 700, "cost_usd": 0.05},
    })
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    res = SdkExecutor().build(_fullstack_req(out))
    assert sorted(fake.targets_seen) == ["api", "web"]          # one session per build-target
    assert res.success is True
    assert res.tokens_in == 3000 and res.tokens_out == 1200      # summed
    assert abs(res.cost_usd - 0.08) < 1e-9                        # summed
    # full scope.json (all targets) still written for verify/acceptance
    full = json.loads((out / ".devagent" / "scope.json").read_text())
    assert {t["name"] for t in full["targets"]} == {"web", "api"}


def test_multi_target_wall_clock_is_max_not_sum(tmp_path, monkeypatch):
    out = tmp_path / "out"
    # serial (cap=1) → calls are (web t0, web end, api t0, api end): web 3s, api 5s.
    times = iter([0.0, 3.0, 0.0, 5.0, 9.0, 9.0])
    monkeypatch.setattr(executor_sdk.time, "monotonic", lambda: next(times))
    fake = _fake_multi(out, {
        "web": {"ok_stream": True}, "api": {"ok_stream": True},
    })
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    # force serial so the injected clock is deterministic
    monkeypatch.setenv("DEVAGENT_BUILD_CONCURRENCY", "1")
    res = SdkExecutor().build(_fullstack_req(out))
    assert res.wall_clock_s == 5.0   # max(target), not 8.0 (the sum)


def test_multi_target_first_failure_surfaces_and_success_false(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_multi(out, {
        "web": {"ok_stream": True},
        "api": {"ok_stream": False, "error": "tsc failed"},
    })
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    res = SdkExecutor().build(_fullstack_req(out))
    assert res.success is False
    assert "tsc failed" in (res.error or "")


def test_multi_target_passes_per_target_plan_slice(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_multi(out, {"web": {"ok_stream": True}, "api": {"ok_stream": True}})
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    SdkExecutor().build(_fullstack_req(out))
    web_plan = json.loads((out / ".devagent" / "build" / "web" / "plan.json").read_text())
    api_plan = json.loads((out / ".devagent" / "build" / "api" / "plan.json").read_text())
    assert [t["id"] for t in web_plan["tasks"]] == ["w"]   # only web-owned tasks
    assert [t["id"] for t in api_plan["tasks"]] == ["a"]   # only api-owned tasks


def test_concurrency_cap_defaults_to_min_targets_3(monkeypatch):
    assert SdkExecutor()._concurrency(2) == 2
    assert SdkExecutor()._concurrency(5) == 3
    monkeypatch.setenv("DEVAGENT_BUILD_CONCURRENCY", "4")
    assert SdkExecutor()._concurrency(5) == 4


# --- M12 review fixes: only parallelize a fresh, cleanly-partitioned plan -------------------

def _targets():
    return [ArtifactSpec(type="frontend", stack="node-vite-react", name="web"),
            ArtifactSpec(type="backend", stack="node-express", name="api")]


def test_partition_clean_when_every_task_maps_to_exactly_one_target():
    plan = Plan(tasks=[Task(id="w", description="x", owned_files=["web/a.ts"]),
                       Task(id="a", description="x", owned_files=["api/b.ts"])])
    slices = SdkExecutor()._partition(plan, _targets())
    assert [t.id for t in slices["web"]] == ["w"]
    assert [t.id for t in slices["api"]] == ["a"]


def test_partition_none_when_a_task_is_unassigned():
    # a root/shared file under no target dir → not a clean partition → sequential
    plan = Plan(tasks=[Task(id="w", description="x", owned_files=["web/a.ts"]),
                       Task(id="a", description="x", owned_files=["api/b.ts"]),
                       Task(id="root", description="x", owned_files=["docker-compose.yml"])])
    assert SdkExecutor()._partition(plan, _targets()) is None


def test_partition_none_when_a_task_spans_two_targets():
    plan = Plan(tasks=[Task(id="w", description="x", owned_files=["web/a.ts"]),
                       Task(id="x", description="x", owned_files=["api/b.ts", "web/c.ts"])])
    assert SdkExecutor()._partition(plan, _targets()) is None


def test_partition_none_when_a_target_owns_no_task():
    plan = Plan(tasks=[Task(id="w", description="x", owned_files=["web/a.ts"]),
                       Task(id="w2", description="x", owned_files=["web/b.ts"])])
    assert SdkExecutor()._partition(plan, _targets()) is None   # api owns nothing


def test_non_partitionable_plan_runs_one_sequential_session(tmp_path, monkeypatch):
    """Bare (non-namespaced) filenames → the whole-project sequential path, NOT N parallel
    sessions clobbering the shared /out."""
    out = tmp_path / "out"
    fake = _fake_ok(out)   # writes .devagent/result.json + web/dist (single path)
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    scope = ProjectScope(title="FS", targets=_targets())
    plan = Plan(tasks=[Task(id="t", description="all", owned_files=["package.json", "server.ts"])])
    SdkExecutor().build(BuildRequest(scope=scope, plan=plan, workdir=str(out), run_id="r1"))
    assert "--target" not in fake.argv                         # one whole-project session
    assert not (out / ".devagent" / "build").exists()          # no per-target split happened


def test_repair_pass_runs_sequential_even_when_partitionable(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fake = _fake_ok(out)
    monkeypatch.setattr(executor_sdk.subprocess, "run", fake)
    import dataclasses
    req = dataclasses.replace(_fullstack_req(out), repair_context="error TS2304")
    SdkExecutor().build(req)
    assert "--target" not in fake.argv                         # repair stays whole-project


def test_stale_per_target_result_is_cleared_before_parallel_build(tmp_path, monkeypatch):
    out = tmp_path / "out"
    # pre-seed a STALE success result for web from a prior run
    stale = out / ".devagent" / "build" / "web"
    stale.mkdir(parents=True)
    (stale / "result.json").write_text(json.dumps({"ok_stream": True, "tokens_in": 999999}))

    def fake_run(argv, **kw):
        tgt = argv[argv.index("--target") + 1]
        bdir = out / ".devagent" / "build" / tgt
        bdir.mkdir(parents=True, exist_ok=True)
        if tgt == "api":                                       # only api writes a fresh result
            bdir.joinpath("result.json").write_text(json.dumps({"ok_stream": True}))
        # web's container "dies" without writing → its stale result must already be gone
        return _Proc()

    monkeypatch.setattr(executor_sdk.subprocess, "run", fake_run)
    res = SdkExecutor().build(_fullstack_req(out))
    assert res.success is False                                # web has no fresh result → not ok
    assert res.tokens_in == 0                                  # the stale 999999 was cleared, not summed
