# devagent/eval/runner.py
"""The eval orchestrator. Per fixture: freeze scope+plan ONCE, then build each arm N times from
those identical bytes, judge + score each run, aggregate. Resumable (each frozen brain and each
run score is cached to disk). The three heavy seams — brain (scope+plan), build+verify, judge —
are INJECTED, so the orchestration unit-tests with fakes (no Docker, no tokens). `build_default_*`
wire the real pipeline for the CLI."""

import dataclasses
import json
from pathlib import Path

from .schema import SESSION_HR_USD_DEFAULT, EvalResult, FixtureResult, RunScore
from .scoring import score_run, summarize_arm


class EvalRunner:
    def __init__(self, eval_dir, corpus, *, brain_fn, build_fn, judge_fn,
                 session_hr_usd: float = SESSION_HR_USD_DEFAULT, arm_available=None):
        self.eval_dir = Path(eval_dir)
        self.corpus = corpus
        self.brain_fn = brain_fn        # (fixture, work_dir) -> (scope, plan, error)
        self.build_fn = build_fn        # (arm, scope, plan, run_dir) -> (build_result, verify_report)
        self.judge_fn = judge_fn        # (scope, repo_path) -> JudgeVerdict | None
        self.session_hr_usd = session_hr_usd
        self.arm_available = arm_available or (lambda arm: True)

    def run(self) -> EvalResult:
        self.eval_dir.mkdir(parents=True, exist_ok=True)
        fixtures = [self.run_fixture(f) for f in self.corpus.fixtures]
        return EvalResult(eval_id=self.eval_dir.name, fixtures=fixtures)

    def run_fixture(self, fixture) -> FixtureResult:
        fdir = self.eval_dir / fixture.name
        fdir.mkdir(parents=True, exist_ok=True)
        scope, plan, err = self._freeze(fixture, fdir)
        if scope is None:
            return FixtureResult(fixture=fixture.name, title=fixture.name, scope_error=err)
        title = getattr(scope, "title", fixture.name)

        runs: list[RunScore] = []
        summaries = []
        for arm in self.corpus.arms:
            if not self.arm_available(arm):
                summaries.append(summarize_arm(arm, [], unavailable=True))
                continue
            arm_runs = [self._run_one(arm, i, scope, plan, fdir) for i in range(self.corpus.n)]
            runs.extend(arm_runs)
            summaries.append(summarize_arm(arm, arm_runs))
        return FixtureResult(fixture=fixture.name, title=title, runs=runs, arms=summaries)

    # -- internals -----------------------------------------------------------

    def _freeze(self, fixture, fdir: Path):
        """Run the brain once; cache scope.json/plan.json so a resume reuses the SAME bytes both
        arms build from. Returns (scope, plan, error)."""
        scope_p, plan_p, err_p = fdir / "scope.json", fdir / "plan.json", fdir / "brain-error.txt"
        if scope_p.is_file() and plan_p.is_file():
            from ..schema import Plan, ProjectScope
            return (ProjectScope.model_validate_json(scope_p.read_text()),
                    Plan.model_validate_json(plan_p.read_text()), None)
        if err_p.is_file():
            return None, None, err_p.read_text()
        scope, plan, err = self.brain_fn(fixture, fdir)
        if scope is None:
            err_p.write_text(err or "scope/plan not produced")
            return None, None, err
        scope_p.write_text(scope.model_dump_json(indent=2))
        plan_p.write_text(plan.model_dump_json(indent=2))
        return scope, plan, None

    def _run_one(self, arm: str, i: int, scope, plan, fdir: Path) -> RunScore:
        """One build run of one arm, cached. A crash in the build/judge is captured as a failed
        RunScore (error set) rather than aborting the corpus."""
        cache = fdir / f"{arm}-{i}.json"
        if cache.is_file():
            return RunScore(**json.loads(cache.read_text()))
        run_dir = fdir / f"{arm}-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            build_result, verify_report = self.build_fn(arm, scope, plan, run_dir)
            verdict = None
            repo = getattr(build_result, "repo_path", None)
            if verify_report is not None and verify_report.build_ok and repo:
                verdict = self.judge_fn(scope, repo)   # judge only a build that actually produced source
            score = score_run(arm, i, build_result, verify_report, verdict, self.session_hr_usd)
        except Exception as e:  # noqa: BLE001 — one run's failure must not abort the corpus
            score = RunScore(arm=arm, run_index=i, acceptance_pass=False, build_ok=False,
                             dist_present=False, checks_total=0, checks_passed=0, error=str(e))
        cache.write_text(json.dumps(dataclasses.asdict(score)))
        return score


# --------------------------------------------------------------------------
# Live wiring for the CLI (not exercised by unit tests — needs Docker + tokens).
# --------------------------------------------------------------------------

def build_default_brain_fn(config):
    """A brain_fn that runs Scope→Plan once via the real phases, gated. Returns (scope, plan, err)."""
    def brain(fixture, work_dir):
        from ..budget import Budget
        from ..ledger import Ledger
        from ..phase_gates import PlanGate, ScopeGate
        from ..phases.base import PhaseContext
        from ..phases.plan import PlanPhase
        from ..phases.scope import ScopePhase
        from ..sandbox import NullSandbox
        ledger = Ledger(work_dir / "brain")
        ctx = PhaseContext(sandbox=NullSandbox(), ledger=ledger,
                           budget=Budget(config.max_tokens, config.max_seconds, config.max_retries,
                                         max_cost_usd=config.max_cost_usd))
        for phase, gate in ((ScopePhase(str(fixture.prd_path)), ScopeGate()), (PlanPhase(), PlanGate())):
            res = phase.run(ctx)
            if res.output_artifact is not None:
                ctx.artifacts[phase.name] = res.output_artifact
            ok, reason = gate.check(res, ctx)
            if not ok:
                return None, None, reason
        return ctx.artifacts["scope"], ctx.artifacts["plan"], None
    return brain


def build_default_build_fn(config):
    """A build_fn that runs the CLI's real BuildPhase per arm (executor + verify + repair loop,
    max 2), returning a cost-carrier (built from the phase meta) and the final VerifyReport.
    Not unit-tested — needs Docker + tokens."""
    def build(arm, scope, plan, run_dir):
        import types

        from .. import egress
        from ..budget import Budget
        from ..executor_sdk import SdkExecutor
        from ..ledger import Ledger
        from ..managed_executor import ManagedExecutor
        from ..phases.base import PhaseContext
        from ..phases.build import BuildPhase
        from ..sandbox import NullSandbox
        from ..verifier import BuildVerifier
        network = proxy = None
        if config.egress:
            network, proxy = egress.ensure()
        executor = (ManagedExecutor() if arm == "managed"
                    else SdkExecutor(network=network, proxy_url=proxy, model=config.build_model))
        out = run_dir / "out"
        budget = Budget(config.max_tokens, config.max_seconds, config.max_retries,
                        max_cost_usd=config.max_cost_usd)
        ctx = PhaseContext(sandbox=NullSandbox(), ledger=Ledger(run_dir), budget=budget,
                           artifacts={"scope": scope, "plan": plan})
        phase = BuildPhase(executor=executor, workdir=str(out), run_id=run_dir.name,
                           verifier=BuildVerifier(network=network, proxy_url=proxy), max_repairs=2)
        res = phase.run(ctx)                    # build → verify → repair loop
        report = res.output_artifact            # VerifyReport (a verifier is set)
        m = res.meta
        build_like = types.SimpleNamespace(
            repo_path=str(out), cost_usd=m.get("cost_usd"),
            wall_clock_s=m.get("wall_clock_s", 0.0) or 0.0,
            tokens_in=m.get("tokens_in", 0) or 0, tokens_out=m.get("tokens_out", 0) or 0,
            cache_read_tokens=0,
            error=(None if getattr(report, "ok", False) else getattr(report, "error", "build failed")))
        return build_like, report
    return build


def build_default_judge_fn():
    from .judge import judge_build, spec_summary
    def judge(scope, repo_path):
        try:
            return judge_build(spec_summary(scope), repo_path)
        except Exception:  # noqa: BLE001 — a judge failure drops the qualitative score, not the run
            return None
    return judge


def default_arm_available(arm: str) -> bool:
    """The managed arm needs the beta sessions API reachable; probe cheaply, degrade gracefully."""
    if arm != "managed":
        return True
    try:
        import anthropic
        return hasattr(anthropic.Anthropic(), "beta")
    except Exception:  # noqa: BLE001
        return False
