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

import json
import sys
from pathlib import Path

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
        backends = [t for t in targets
                    if recipes_mod.get(t.stack).kind == "build" and t.type != "frontend"]
        frontends = [t for t in targets if t.type == "frontend"]

        urls: dict[str, str] = {}
        health_paths: dict[str, str] = {}
        services: dict[str, str] = {}
        failed: list[str] = []

        network = None
        if datastores:
            network = PREVIEW_NETWORK
            deploy.ensure_network(network)

        # 1. datastores first
        for t in datastores:
            cname = self.start_service(t, network=network)
            if cname:
                services[t.name] = cname
            else:
                failed.append(t.name)

        # 2. backends — inject the resolved connection URL for any declared datastore
        svc_recipe = {t.name: recipes_mod.get(t.stack).service for t in datastores}
        for t in backends:
            env: dict = {}
            ds = (t.detail or {}).get("datastore")
            if ds and ds in svc_recipe:
                conn_env = (t.detail or {}).get("conn_env", "DATABASE_URL")
                env[conn_env] = svc_recipe[ds].conn_url_template.format(
                    host=ds, port=svc_recipe[ds].port)
            url = self.start_target(self.workdir, t, network=network, env=env or None)
            if url:
                urls[t.name] = url
                recipe = recipes_mod.get(t.stack)
                health_paths[t.name] = recipe.boot.health_path if recipe.boot else "/"
            else:
                failed.append(t.name)

        # 3. frontends — wired to the first backend via /config.json (unchanged)
        backend_url = next(iter(urls.values()), "") if urls else ""
        for t in frontends:
            if backend_url:
                dist_dir = Path(self.workdir) / t.name / "dist"
                if dist_dir.exists():
                    (dist_dir / "config.json").write_text(
                        json.dumps({"apiBase": backend_url}), encoding="utf-8")
                else:
                    print(f"warning: frontend {t.name} has no dist/ — skipping config.json "
                          "(apiBase wiring lost)", file=sys.stderr)
            url = self.start_target(self.workdir, t)
            if url:
                urls[t.name] = url
                health_paths[t.name] = "/"
            else:
                failed.append(t.name)

        primary_url = ""
        for t in frontends + backends:
            if t.name in urls:
                primary_url = urls[t.name]
                break

        result = deploy.DeployResult(url=primary_url, urls=urls, health_paths=health_paths,
                                     services=services)
        all_ok = not failed and bool(urls)
        output = primary_url if all_ok else f"deploy failed for: {', '.join(failed)}"
        return PhaseResult(name=self.name, exit_code=0 if all_ok else 1, output=output,
                           meta={"url": primary_url, "urls": urls, "services": services},
                           output_artifact=result)
