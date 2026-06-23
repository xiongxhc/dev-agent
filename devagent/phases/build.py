"""BuildPhase — the adapter that plugs an Executor into the phase pipeline.

It reads the frozen Spec+Plan from ctx.artifacts, hands them to the injected Executor
(SdkExecutor in M2, ManagedExecutor in M4 — the A/B seam), and wraps the BuildResult as
the phase output_artifact so the deterministic BuildGate can re-check the produced repo.

Tokens the executor reports are folded into the shared Budget so the build counts against
the same ceilings as the brain phases (and a runaway build aborts the run via BudgetExceeded,
exactly like any other phase). The phase does NOT use ctx.sandbox: SdkExecutor self-contains
its own disposable Docker container.
"""

from ..executor import BuildRequest, Executor
from .base import PhaseContext, PhaseResult


class BuildPhase:
    name = "build"

    def __init__(self, executor: Executor, workdir: str, run_id: str):
        self.executor = executor
        self.workdir = workdir
        self.run_id = run_id

    def run(self, ctx: PhaseContext) -> PhaseResult:
        req = BuildRequest(
            spec=ctx.artifacts["spec"],
            plan=ctx.artifacts["plan"],
            workdir=self.workdir,
            run_id=self.run_id,
            budget=ctx.budget,
        )
        result = self.executor.build(req)
        # Account spent tokens against the shared ceiling (may raise BudgetExceeded).
        ctx.budget.add_tokens(result.tokens_in + result.tokens_out)
        return PhaseResult(
            name=self.name,
            exit_code=0 if result.success else 1,
            output=f"built {result.repo_path}" if result.success else (result.error or "build failed"),
            meta={
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost_usd": result.cost_usd,
                "wall_clock_s": result.wall_clock_s,
                "repo_path": result.repo_path,
                "transcript_path": result.transcript_path,
            },
            output_artifact=result,
        )
