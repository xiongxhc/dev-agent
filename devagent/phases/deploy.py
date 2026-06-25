"""DeployPhase — plugs the local preview start into the phase pipeline. It starts a detached
container serving the built bundle and returns the preview URL (gated by DeployGate, which
proves the URL answers). It does NOT use ctx.sandbox: start_preview self-contains its own
container (like SdkExecutor/BuildVerifier).

M6 extension: when ctx.artifacts["scope"] is present, iterates all targets and calls
start_target per target. Backend targets start first so the frontend can receive the backend
URL via /config.json. DeployResult.urls maps each target name to its URL; .url is set to the
frontend's URL (or first URL if no frontend) as the primary entry point."""

import json
from pathlib import Path

from .. import deploy
from .base import PhaseContext, PhaseResult


class DeployPhase:
    name = "deploy"

    def __init__(self, workdir: str, start=None, start_target=None):
        self.workdir = workdir
        self.start = start or deploy.start_preview
        self.start_target = start_target or deploy.start_target

    def run(self, ctx: PhaseContext) -> PhaseResult:
        scope = ctx.artifacts.get("scope") if ctx is not None else None

        if scope is None:
            # Legacy single-target path (backward-compat for existing tests)
            result = self.start(self.workdir)
            return PhaseResult(
                name=self.name,
                exit_code=0 if result.url else 1,
                output=result.url or (result.error or "deploy failed"),
                meta={"url": result.url},
                output_artifact=result,
            )

        # M6: per-target preview — start backends first, then frontends
        targets = list(scope.targets)
        backends = [t for t in targets if t.type != "frontend"]
        frontends = [t for t in targets if t.type == "frontend"]

        urls: dict[str, str] = {}
        failed: list[str] = []

        # Start backends first; collect URLs so the frontend can be wired to them
        for target in backends:
            url = self.start_target(self.workdir, target)
            if url:
                urls[target.name] = url
            else:
                failed.append(target.name)

        # Write /config.json into each frontend's dist before starting the static server
        backend_url = next(iter(urls.values()), "") if urls else ""
        for target in frontends:
            if backend_url:
                dist_dir = Path(self.workdir) / target.name / "dist"
                if dist_dir.exists():
                    (dist_dir / "config.json").write_text(
                        json.dumps({"apiBase": backend_url}), encoding="utf-8"
                    )
            url = self.start_target(self.workdir, target)
            if url:
                urls[target.name] = url
            else:
                failed.append(target.name)

        # Primary URL: frontend first, else first backend
        primary_url = ""
        for t in frontends + backends:
            if t.name in urls:
                primary_url = urls[t.name]
                break

        result = deploy.DeployResult(url=primary_url, urls=urls)
        all_ok = not failed and bool(urls)
        output = primary_url if all_ok else f"deploy failed for: {', '.join(failed)}"
        return PhaseResult(
            name=self.name,
            exit_code=0 if all_ok else 1,
            output=output,
            meta={"url": primary_url, "urls": urls},
            output_artifact=result,
        )
