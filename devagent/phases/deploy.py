"""DeployPhase — plugs the local preview start into the phase pipeline. It starts a detached
container serving the built bundle and returns the preview URL (gated by DeployGate, which
proves the URL answers). It does NOT use ctx.sandbox: start_preview self-contains its own
container (like SdkExecutor/BuildVerifier).

M6 extension: when ctx.artifacts["scope"] is present, iterates all targets and calls
start_target per target. Backend targets start first so the frontend can receive the backend
URL via /config.json. DeployResult.urls maps each target name to its URL; .url is set to the
frontend's URL (or first URL if no frontend) as the primary entry point.

M7 extension: when datastores are present, they start first (start_service), a shared network
is created, backends join the network and receive conn-env (DATABASE_URL etc.), then frontends
are wired via /config.json as before."""

from .. import deploy
from .. import recipes as recipes_mod
from .base import PhaseContext, PhaseResult

PREVIEW_NETWORK = "devagent-preview-net"


class DeployPhase:
    name = "deploy"

    def __init__(self, workdir: str, start=None, start_target=None, start_service=None):
        self.workdir = workdir
        self.start = start or deploy.start_preview
        self.start_target = start_target or deploy.start_target
        self.start_service = start_service or deploy.start_service

    def run(self, ctx: PhaseContext) -> PhaseResult:
        scope = ctx.artifacts.get("scope") if ctx is not None else None
        if scope is None:
            result = self.start(self.workdir)
            return PhaseResult(name=self.name, exit_code=0 if result.url else 1,
                               output=result.url or (result.error or "deploy failed"),
                               meta={"url": result.url}, output_artifact=result)

        targets = list(scope.targets)
        datastores = [t for t in targets if recipes_mod.get(t.stack).kind == "service"]
        network = None
        if datastores:
            network = PREVIEW_NETWORK
            deploy.ensure_network(network)
            # Reclaim orphaned preview datastore volumes from dropped/renamed targets (design §6).
            deploy.sweep_preview_volumes({t.name for t in datastores})

        wired = deploy.wire_targets(targets, self.workdir, network=network,
                                    start_target_fn=self.start_target,
                                    start_service_fn=self.start_service)

        result = deploy.DeployResult(url=wired.primary_url, urls=wired.urls,
                                     health_paths=wired.health_paths, services=wired.services)
        all_ok = not wired.failed and bool(wired.urls)
        output = wired.primary_url if all_ok else f"deploy failed for: {', '.join(wired.failed)}"
        return PhaseResult(name=self.name, exit_code=0 if all_ok else 1, output=output,
                           meta={"url": wired.primary_url, "urls": wired.urls,
                                 "services": wired.services},
                           output_artifact=result)
