"""DeployPhase — plugs the local preview start into the phase pipeline. It starts a detached
container serving the built bundle and returns the preview URL (gated by DeployGate, which
proves the URL answers). It does NOT use ctx.sandbox: start_preview self-contains its own
container (like SdkExecutor/BuildVerifier)."""

from .. import deploy
from .base import PhaseContext, PhaseResult


class DeployPhase:
    name = "deploy"

    def __init__(self, workdir: str, start=None):
        self.workdir = workdir
        self.start = start or deploy.start_preview

    def run(self, ctx: PhaseContext) -> PhaseResult:
        result = self.start(self.workdir)
        return PhaseResult(
            name=self.name,
            exit_code=0 if result.url else 1,
            output=result.url or (result.error or "deploy failed"),
            meta={"url": result.url},
            output_artifact=result,
        )
