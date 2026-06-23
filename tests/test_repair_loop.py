"""The repair loop lives INSIDE BuildPhase (per the orchestrator design note): build ->
verify -> if failing, re-invoke the executor with the verify diagnostics, up to
max_repairs, spending a shared-Budget retry each time. When a verifier is supplied the
phase emits the final VerifyReport (gated by VerifyGate); with no verifier it keeps the
plain BuildResult contract. No Docker, no tokens — executor + verifier are fakes."""

from devagent.budget import Budget
from devagent.executor import BuildResult
from devagent.ledger import Ledger
from devagent.phases.base import PhaseContext
from devagent.phases.build import BuildPhase
from devagent.schema import AcceptanceCheck, Plan, Spec, Task
from devagent.verifier import VerifyReport


def _ctx(tmp_path, budget=None):
    budget = budget or Budget(10**9, 1e9, max_retries=9)
    ctx = PhaseContext(sandbox=None, budget=budget, ledger=Ledger(tmp_path / "run"))
    ctx.artifacts["spec"] = Spec(title="Hello", pages=["/"],
                                 acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])
    ctx.artifacts["plan"] = Plan(tasks=[Task(id="t", description="x", owned_files=["package.json"])])
    return ctx


class ScriptedExecutor:
    def __init__(self):
        self.reqs = []

    def build(self, req):
        self.reqs.append(req)
        return BuildResult(repo_path=req.workdir, success=True, tokens_in=100, tokens_out=50)


class ScriptedVerifier:
    """Returns the queued reports in order; repeats the last once exhausted."""

    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0

    def verify(self, req):
        self.calls += 1
        rep = self.reports[0] if len(self.reports) == 1 else self.reports.pop(0)
        return rep


_OK = VerifyReport(build_ok=True, dist_present=True, exit_code=0)
def _bad(tail="TS2304"):
    return VerifyReport(build_ok=False, dist_present=False, exit_code=1, log_tail=tail)


def test_passes_first_try_no_repair(tmp_path):
    ex, vf = ScriptedExecutor(), ScriptedVerifier([_OK])
    budget = Budget(10**9, 1e9, 9)
    phase = BuildPhase(executor=ex, workdir="/out", run_id="r", verifier=vf, max_repairs=2)
    result = phase.run(_ctx(tmp_path, budget))
    assert len(ex.reqs) == 1          # built once
    assert budget.retries == 0        # no retry spent
    assert result.exit_code == 0
    assert isinstance(result.output_artifact, VerifyReport)


def test_repairs_once_then_succeeds(tmp_path):
    ex = ScriptedExecutor()
    vf = ScriptedVerifier([_bad("error: missing import App"), _OK])
    budget = Budget(10**9, 1e9, 9)
    phase = BuildPhase(executor=ex, workdir="/out", run_id="r", verifier=vf, max_repairs=2)
    result = phase.run(_ctx(tmp_path, budget))
    assert len(ex.reqs) == 2          # initial build + one repair
    assert budget.retries == 1        # one retry spent
    # the repair build is fed the failing verify diagnostics
    assert ex.reqs[1].repair_context is not None
    assert "missing import App" in ex.reqs[1].repair_context
    assert ex.reqs[0].repair_context is None  # the first build is not a repair
    assert result.exit_code == 0


def test_exhausts_repairs_and_reports_failure(tmp_path):
    ex = ScriptedExecutor()
    vf = ScriptedVerifier([_bad()])   # always fails
    budget = Budget(10**9, 1e9, 9)
    phase = BuildPhase(executor=ex, workdir="/out", run_id="r", verifier=vf, max_repairs=2)
    result = phase.run(_ctx(tmp_path, budget))
    assert len(ex.reqs) == 3          # initial + 2 repairs (cap)
    assert budget.retries == 2
    assert result.exit_code == 1
    assert result.output_artifact.build_ok is False
