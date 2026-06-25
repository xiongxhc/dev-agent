"""BuildPhase — the adapter that plugs an Executor into the phase pipeline, and the home
of the M2 repair loop.

It reads the frozen ProjectScope+Plan from ctx.artifacts and hands them to the injected Executor
(SdkExecutor in M2, ManagedExecutor in M4 — the A/B seam). Tokens the executor reports are
folded into the shared Budget so the build counts against the same ceilings as the brain
phases (a runaway build aborts the run via BudgetExceeded, like any phase).

If a `verifier` is supplied, the phase also owns the **repair loop** (per the orchestrator
design note: repair lives in the build phase, not the orchestrator). It rebuilds-from-source
to check the executor's output, and on failure re-invokes the executor with the verify
diagnostics, up to `max_repairs`, spending a shared-Budget retry each time. It then emits the
final VerifyReport (gated by VerifyGate). With no verifier it keeps the plain BuildResult
contract (gated by BuildGate). The phase never uses ctx.sandbox: both SdkExecutor and
BuildVerifier self-contain their own disposable Docker containers.
"""

from dataclasses import replace

from ..executor import BuildRequest, Executor
from ..verifier import VerifyRequest
from .base import PhaseContext, PhaseResult


class BuildPhase:
    name = "build"

    def __init__(self, executor: Executor, workdir: str, run_id: str,
                 verifier=None, max_repairs: int = 2):
        self.executor = executor
        self.workdir = workdir
        self.run_id = run_id
        self.verifier = verifier
        self.max_repairs = max_repairs

    def run(self, ctx: PhaseContext) -> PhaseResult:
        req = BuildRequest(
            scope=ctx.artifacts["scope"],
            plan=ctx.artifacts["plan"],
            workdir=self.workdir,
            run_id=self.run_id,
            budget=ctx.budget,
        )
        result = self._build(ctx, req)

        if self.verifier is None:
            # No re-verification: emit the executor's own (untrusted) BuildResult; BuildGate
            # re-checks the produced repo on disk.
            return PhaseResult(
                name=self.name,
                exit_code=0 if result.success else 1,
                output=f"built {result.repo_path}" if result.success else (result.error or "build failed"),
                meta=self._build_meta(result),
                output_artifact=result,
            )

        vreq = VerifyRequest(workdir=self.workdir, run_id=self.run_id)
        report = self.verifier.verify(vreq)
        repairs = 0
        while not report.ok and repairs < self.max_repairs:
            repairs += 1
            ctx.budget.spend_retry()  # shared retry ceiling (may raise BudgetExceeded)
            result = self._build(ctx, replace(req, repair_context=report.log_tail))
            report = self.verifier.verify(vreq)

        meta = self._build_meta(result)
        meta.update({"repairs": repairs, "verify_exit": report.exit_code,
                     "build_ok": report.build_ok, "checks_pass": report.checks_pass})
        return PhaseResult(
            name=self.name,
            exit_code=0 if report.ok else 1,
            output=f"built {result.repo_path}" if report.ok else (report.error or "failed verification"),
            meta=meta,
            output_artifact=report,
        )

    def _build(self, ctx: PhaseContext, req: BuildRequest):
        result = self.executor.build(req)
        # Account spent tokens against the shared ceiling (may raise BudgetExceeded).
        ctx.budget.add_tokens(result.tokens_in + result.tokens_out)
        return result

    @staticmethod
    def _build_meta(result) -> dict:
        return {
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
            "wall_clock_s": result.wall_clock_s,
            "repo_path": result.repo_path,
            "transcript_path": result.transcript_path,
        }
