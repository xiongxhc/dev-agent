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


def _write_dist(workdir):
    # BuildGate still checks dist/index.html at the top of workdir (updated in a later task)
    dist = workdir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html></html>")


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

def test_build_gate_passes_when_dist_index_present(tmp_path):
    out = tmp_path / "out"
    _write_dist(out)
    from devagent.phases.base import PhaseResult
    res = PhaseResult("build", 0, output_artifact=BuildResult(repo_path=str(out), success=True))
    assert BuildGate().check(res).ok is True


def test_build_gate_fails_when_executor_claims_success_but_no_dist(tmp_path):
    # The anti-trust property: a lying executor (success=True, nothing built) is caught.
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
