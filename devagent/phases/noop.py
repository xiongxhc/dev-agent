"""No-op phase: execs a marker echo inside the sandbox. Its only job in M1 is to
prove the orchestrator -> sandbox -> gate path end to end without spending tokens."""

from .base import Phase, PhaseContext, PhaseResult


class NoopPhase:
    name = "noop"
    MARKER = "devagent-m1-ok"

    def run(self, ctx: PhaseContext) -> PhaseResult:
        exit_code, out, err = ctx.sandbox.run(["echo", self.MARKER])
        return PhaseResult(
            name=self.name,
            exit_code=exit_code,
            output=out.strip(),
            meta={"stderr": err.strip()},
        )
