"""VerifyPhase — re-verifies the executor's output by rebuilding from source in a clean
container (via the injected verifier) and emitting a VerifyReport for the VerifyGate.

Shared across both A/B arms (verify is not part of the swappable seam). Like BuildPhase it
ignores ctx.sandbox: BuildVerifier self-contains its own disposable container. No tokens —
the rebuild runs no model — so nothing is charged to the Budget here.
"""

from ..verifier import VerifyRequest
from .base import PhaseContext, PhaseResult


class VerifyPhase:
    name = "verify"

    def __init__(self, verifier, workdir: str, run_id: str):
        self.verifier = verifier
        self.workdir = workdir
        self.run_id = run_id

    def run(self, ctx: PhaseContext) -> PhaseResult:
        report = self.verifier.verify(VerifyRequest(workdir=self.workdir, run_id=self.run_id))
        return PhaseResult(
            name=self.name,
            exit_code=0 if report.build_ok else 1,
            output="rebuilt from source" if report.build_ok else (report.error or "verify failed"),
            meta={
                "build_ok": report.build_ok,
                "dist_present": report.dist_present,
                "exit_code": report.exit_code,
                "wall_clock_s": report.wall_clock_s,
                "log_tail": report.log_tail,
            },
            output_artifact=report,
        )
