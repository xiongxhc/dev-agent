"""BuildPhase (Executor->Phase adapter) + BuildGate (deterministic on-disk re-check).

No Docker, no tokens: a FakeExecutor stands in for SdkExecutor. The BuildGate must
re-check the produced repo on disk and NOT trust BuildResult.success (the fairness rule).
"""

from devagent.budget import Budget
from devagent.executor import BuildRequest, BuildResult
from devagent.ledger import Ledger
from devagent.phase_gates import BuildGate
from devagent.phases.base import PhaseContext
from devagent.phases.build import BuildPhase
from devagent.schema import AcceptanceCheck, ArtifactSpec, Plan, ProjectScope, Task


def _scope_plan():
    scope = ProjectScope(
        title="Hello",
        targets=[ArtifactSpec(
            type="frontend", stack="node-vite-react", name="web", detail={},
            acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")],
        )],
    )
    plan = Plan(tasks=[Task(id="t1", description="scaffold", owned_files=["package.json"])])
    return scope, plan


def _ctx(tmp_path, scope, plan, budget=None):
    budget = budget or Budget(10**9, 1e9, 9)
    ctx = PhaseContext(sandbox=None, budget=budget, ledger=Ledger(tmp_path / "run"))
    ctx.artifacts["scope"] = scope
    ctx.artifacts["plan"] = plan
    return ctx


class FakeExecutor:
    """Records the request and returns a canned BuildResult; writes no files."""

    def __init__(self, result: BuildResult):
        self.result = result
        self.seen: BuildRequest | None = None

    def build(self, req: BuildRequest) -> BuildResult:
        self.seen = req
        return self.result


_SCOPE_JSON = {
    "title": "Hello",
    "targets": [
        {"name": "web", "stack": "node-vite-react", "artifact_glob": "dist/index.html"},
    ],
}


def _write_scope(workdir: "Path") -> None:
    """Seed a one-target scope.json under <workdir>/.devagent/."""
    dev = workdir / ".devagent"
    dev.mkdir(parents=True, exist_ok=True)
    import json
    (dev / "scope.json").write_text(json.dumps(_SCOPE_JSON))


def _write_artifact(workdir: "Path") -> None:
    """Write the per-target artifact at <workdir>/web/dist/index.html."""
    artifact = workdir / "web" / "dist"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "index.html").write_text("<html></html>")


# --- BuildPhase ----------------------------------------------------------------

def test_build_phase_passes_frozen_scope_plan_workdir_runid_to_executor(tmp_path):
    scope, plan = _scope_plan()
    out = tmp_path / "out"
    ex = FakeExecutor(BuildResult(repo_path=str(out), success=True))
    phase = BuildPhase(executor=ex, workdir=str(out), run_id="run-xyz")

    result = phase.run(_ctx(tmp_path, scope, plan))

    assert ex.seen is not None
    assert ex.seen.scope is scope and ex.seen.plan is plan
    assert ex.seen.workdir == str(out)
    assert ex.seen.run_id == "run-xyz"
    assert result.name == "build"
    assert result.exit_code == 0
    assert isinstance(result.output_artifact, BuildResult)


def test_build_phase_exit_1_when_executor_claims_failure(tmp_path):
    scope, plan = _scope_plan()
    out = tmp_path / "out"
    ex = FakeExecutor(BuildResult(repo_path=str(out), success=False, error="boom"))
    result = BuildPhase(executor=ex, workdir=str(out), run_id="r").run(_ctx(tmp_path, scope, plan))
    assert result.exit_code == 1
    assert "boom" in result.output


def test_build_phase_accounts_executor_tokens_into_shared_budget(tmp_path):
    scope, plan = _scope_plan()
    out = tmp_path / "out"
    ex = FakeExecutor(BuildResult(repo_path=str(out), success=True, tokens_in=1200, tokens_out=800))
    budget = Budget(10**9, 1e9, 9)
    BuildPhase(executor=ex, workdir=str(out), run_id="r").run(_ctx(tmp_path, scope, plan, budget))
    assert budget.tokens == 2000


def test_build_phase_excludes_cache_read_from_runaway_ceiling(tmp_path):
    """Cache-read tokens (~0.1x cost) must NOT count against the runaway ceiling. A legitimate
    cache-heavy build (the persistence live run: 1.39M token_in, 1.33M of it cache-read, $1.80)
    must spend only the ~92k expensive tokens, not trip a 1M ceiling."""
    scope, plan = _scope_plan()
    out = tmp_path / "out"
    ex = FakeExecutor(BuildResult(repo_path=str(out), success=True,
                                  tokens_in=1_388_702, tokens_out=30_267,
                                  cache_read_tokens=1_327_201))
    budget = Budget(1_000_000, 1e9, 9)        # the exact ceiling the live run tripped
    BuildPhase(executor=ex, workdir=str(out), run_id="r").run(_ctx(tmp_path, scope, plan, budget))
    assert budget.tokens == 1_388_702 + 30_267 - 1_327_201   # expensive tokens only = 91,768
    assert budget.tokens < 1_000_000                          # did NOT trip the runaway guard


def test_build_phase_carries_cost_and_wallclock_in_meta(tmp_path):
    scope, plan = _scope_plan()
    out = tmp_path / "out"
    ex = FakeExecutor(BuildResult(repo_path=str(out), success=True,
                                  tokens_in=10, tokens_out=5, cost_usd=0.21, wall_clock_s=28.0))
    result = BuildPhase(executor=ex, workdir=str(out), run_id="r").run(_ctx(tmp_path, scope, plan))
    assert result.meta["cost_usd"] == 0.21
    assert result.meta["wall_clock_s"] == 28.0
    assert result.meta["tokens_in"] == 10 and result.meta["tokens_out"] == 5


# --- BuildGate (re-checks disk; does NOT trust BuildResult.success) -------------

def test_build_gate_passes_when_per_target_artifact_present(tmp_path):
    """Per-target check: scope.json exists and web/dist/index.html is present."""
    out = tmp_path / "out"
    _write_scope(out)
    _write_artifact(out)
    from devagent.phases.base import PhaseResult
    res = PhaseResult("build", 0, output_artifact=BuildResult(repo_path=str(out), success=True))
    assert BuildGate().check(res).ok is True


def test_build_gate_fails_when_executor_claims_success_but_no_artifact(tmp_path):
    """Anti-trust property: scope.json exists but artifact is absent — lying executor caught."""
    out = tmp_path / "out"
    _write_scope(out)
    # Do NOT write the artifact
    from devagent.phases.base import PhaseResult
    res = PhaseResult("build", 0, output_artifact=BuildResult(repo_path=str(out), success=True))
    gr = BuildGate().check(res)
    assert gr.ok is False
    assert "web" in gr.reason


def test_build_gate_legacy_fallback_passes_with_dist_index(tmp_path):
    """Legacy path (no scope.json): flat dist/index.html is sufficient."""
    out = tmp_path / "out"
    dist = out / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html></html>")
    from devagent.phases.base import PhaseResult
    res = PhaseResult("build", 0, output_artifact=BuildResult(repo_path=str(out), success=True))
    assert BuildGate().check(res).ok is True


def test_build_gate_legacy_fallback_fails_when_no_dist(tmp_path):
    """Legacy path (no scope.json): missing dist/index.html fails."""
    out = tmp_path / "out"
    out.mkdir()
    from devagent.phases.base import PhaseResult
    res = PhaseResult("build", 0, output_artifact=BuildResult(repo_path=str(out), success=True))
    gr = BuildGate().check(res)
    assert gr.ok is False
    assert "dist" in gr.reason


def test_build_gate_fails_when_no_artifact(tmp_path):
    from devagent.phases.base import PhaseResult
    res = PhaseResult("build", 1, output="executor crashed")
    assert BuildGate().check(res).ok is False
