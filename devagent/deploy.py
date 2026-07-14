"""M3 local preview — `deploy → preview URL`. Starts a detached container that serves the
built bundle (/out/dist) via preview_server.py on a host port, and exposes a DeployGate that
proves the URL actually answers HTTP 200.

Unlike SdkExecutor/BuildVerifier this needs no API key — a static server runs no model, so
nothing secret is passed to the container (the docker argv carries no key). Reuses the M2
image (python3 only) and the same `--user 1000:1000`, `:ro`-mount argv style.

M6 extension: `start_target(out_dir, target)` starts one target per recipe type — backend
targets are run detached via docker on a host port; frontend targets are served statically
via preview_server.py. Backend(s) start first; the frontend receives a /config.json with the
backend's URL so the pre-built bundle can discover the API base at runtime."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .gates import GateResult

DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
PREVIEW_SERVER = Path(__file__).parent / "preview_server.py"
CONTAINER = "devagent-preview"


@dataclass
class DeployResult:
    url: str
    container: str | None = None
    error: str | None = None
    urls: dict[str, str] = field(default_factory=dict)
    health_paths: dict[str, str] = field(default_factory=dict)
    services: dict[str, str] = field(default_factory=dict)   # datastore name -> container (not HTTP)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_preview(out_dir, image: str = DEFAULT_IMAGE, port: int | None = None) -> DeployResult:
    out = Path(out_dir).resolve()
    port = port or int(os.getenv("DEVAGENT_PREVIEW_PORT") or 0) or _free_port()
    # replace any prior preview so the name is free (ignore errors — none may exist)
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, text=True)
    argv = [
        "docker", "run", "-d", "--restart", "unless-stopped", "--name", CONTAINER,
        "-p", f"{port}:8000",
        "--user", "1000:1000",
        "-v", f"{out}:/out:ro",
        "-v", f"{PREVIEW_SERVER}:/preview.py:ro",
        image,
        "python3", "/preview.py",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        return DeployResult(url="", error=(proc.stderr or proc.stdout or "docker run failed").strip())
    return DeployResult(url=f"http://localhost:{port}", container=CONTAINER)


def stop_preview() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, text=True)


def ensure_network(name: str) -> None:
    if subprocess.run(["docker", "network", "inspect", name],
                      capture_output=True, text=True).returncode != 0:
        subprocess.run(["docker", "network", "create", name], capture_output=True, text=True)


def sweep_preview_volumes(keep: set[str]) -> None:
    """Reclaim orphaned preview datastore volumes (design §6 volume hygiene).

    Removes every `devagent-preview-<name>-data` volume whose `<name>` is NOT in *keep*
    (the current scope's datastore target names) — i.e. volumes left behind by datastore
    targets that were renamed or dropped. A volume still attached to a running preview
    container cannot be removed (docker refuses), so live previews are never disturbed."""
    listing = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                             capture_output=True, text=True)
    for name in (listing.stdout or "").splitlines():
        name = name.strip()
        if not (name.startswith("devagent-preview-") and name.endswith("-data")):
            continue
        target = name[len("devagent-preview-"):-len("-data")]
        if target not in keep:
            subprocess.run(["docker", "volume", "rm", name], capture_output=True, text=True)


def _wait_service_ready(container: str, ready_cmd, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = subprocess.run(["docker", "exec", container, *ready_cmd],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return True
        time.sleep(1.0)
    return False


def start_service(target, image: str = DEFAULT_IMAGE, network: str | None = None,
                  alias: str | None = None, container_name: str | None = None,
                  preserve_volume: bool = False) -> str | None:
    """Start a datastore sibling container for preview; return its container name or None.
    A named volume at the engine's data dir makes the data survive `docker restart`.
    alias/container_name (M21) let system bring-up namespace intra-node datastores per
    service node; defaults preserve the single-run preview naming. preserve_volume=True
    (M25) skips dropping the prior named volume, for in-place updates that must keep data."""
    from .recipes import get as get_recipe

    svc = get_recipe(target.stack).service
    container_name = container_name or f"devagent-preview-{target.name}"
    alias = alias or target.name
    vol = f"{container_name}-data"
    env_flags = []
    for k, v in svc.env:
        env_flags += ["-e", f"{k}={v}"]
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
    if not preserve_volume:
        # Fresh build deploy = fresh datastore: drop the prior named volume so stale data
        # can't silently carry over. M25 update passes preserve_volume=True when the
        # db_schema contract is UNCHANGED — the point of an in-place update is that the
        # data survives the container replacement.
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True, text=True)
    argv = ["docker", "run", "-d", "--restart", "unless-stopped", "--name", container_name]
    if network:
        argv += ["--network", network, "--network-alias", alias]
    argv += [*env_flags, "-v", f"{vol}:{svc.volume_path}", svc.image]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    if not _wait_service_ready(container_name, svc.ready_cmd, svc.ready_timeout_s):
        return None
    return container_name


def start_target(out_dir: str, target, image: str = DEFAULT_IMAGE,
                 network: str | None = None, env: dict | None = None,
                 alias: str | None = None, container_name: str | None = None) -> str | None:
    """Start one target for local preview; return its URL or None on failure.

    Backend targets (recipe has a BootSpec) run detached via `docker run -d` on a free host
    port. Frontend targets (no BootSpec) are served statically via preview_server.py.
    Returns the URL string so the caller can aggregate into DeployResult.urls, or None if the
    start failed.
    """
    from .recipes import get as get_recipe

    out = Path(out_dir).resolve()
    recipe = get_recipe(target.stack)
    boot = recipe.boot
    container_name = container_name or f"devagent-preview-{target.name}"
    alias = alias or target.name

    if boot is not None:
        # --- backend: run the built service detached ---
        host_port = _free_port()
        target_dir = out / target.name
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
        env_flags = []
        for k, v in (env or {}).items():
            env_flags += ["-e", f"{k}={v}"]
        net_flags = ["--network", network, "--network-alias", alias] if network else []
        argv = [
            "docker", "run", "-d", "--restart", "unless-stopped", "--name", container_name,
            "-p", f"{host_port}:{boot.port}",
            *net_flags, *env_flags,
            "--user", "1000:1000",
            "-v", f"{target_dir}:/out/{target.name}",
            "-w", f"/out/{target.name}",
            image,
            *boot.cmd,
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return f"http://127.0.0.1:{host_port}"
    else:
        # --- frontend: serve <name>/dist statically (unchanged) ---
        host_port = _free_port()
        dist_dir = out / target.name / "dist"
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
        argv = [
            "docker", "run", "-d", "--restart", "unless-stopped", "--name", container_name,
            "-p", f"{host_port}:8000",
            "--user", "1000:1000",
            "-v", f"{dist_dir}:/out/dist:ro",
            "-v", f"{PREVIEW_SERVER}:/preview.py:ro",
            image,
            "python3", "/preview.py",
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return f"http://127.0.0.1:{host_port}"


def _probe(full_url: str, timeout: float = 10.0) -> bool:
    """Poll *full_url* until it returns HTTP 200 or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(full_url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


@dataclass
class DeployGate:
    """Passes iff every started target answers HTTP 200. A freshly-started container needs a
    moment to bind, so each probe polls for up to ~10s before failing."""

    name: str = "preview_responds"

    def check(self, result) -> GateResult:
        if result.exit_code != 0:
            return GateResult(False, f"{result.name} exited {result.exit_code}: {result.output!r}")
        art = result.output_artifact
        if art is None:
            return GateResult(False, f"{result.name} produced no output_artifact")
        if not art.url:
            return GateResult(False, "deploy produced no url")

        # Multi-target path: probe EACH target; gate passes iff ALL answer 200.
        if art.urls:
            for name, url in art.urls.items():
                health_path = art.health_paths.get(name, "/")
                full_url = url.rstrip("/") + health_path
                if not _probe(full_url):
                    return GateResult(False, f"target '{name}' did not return 200 at {full_url}")
            return GateResult(True)

        # Legacy single-target path (backward-compat): probe art.url directly.
        if not _probe(art.url):
            return GateResult(False, f"preview did not return 200: {art.url}")
        return GateResult(True)


@dataclass
class WiredTargets:
    urls: dict            # target name -> URL
    health_paths: dict    # target name -> health path
    services: dict        # datastore target name -> container name
    containers: list      # every container started, in start order (caller-side teardown)
    failed: list          # target names that failed to start
    primary_url: str      # frontend-first primary entry point ("" if none)


def wire_targets(targets, workdir, *, network=None, alias_prefix="", extra_env=None,
                 frontend_api_base=None, start_target_fn=None, start_service_fn=None,
                 container_prefix=None) -> WiredTargets:
    """One service's target set -> running, wired containers (M21: extracted from DeployPhase
    so system bring-up reuses the exact intra-scope wiring: datastores first, conn-env
    injection for backends, frontend dist/config.json -> apiBase).

    alias_prefix namespaces container names + network aliases (and the conn-URL hosts, which
    must match the aliases) so two nodes' same-named internal targets can't collide on a
    shared network; with the default "" every call is byte-identical to the pre-M21
    DeployPhase loop. container_prefix (one-flow, 2026-07-06) overrides the container-NAME
    prefix only — system bring-up passes a per-run prefix so a kept-up preview can't be
    docker-rm-f'd by the next run's bring-up; aliases stay alias_prefix-based (they are the
    conn-URL hosts). extra_env seeds each backend's env (a target's own detail wiring wins).
    frontend_api_base supplies apiBase when this scope has no internal backend."""
    from .recipes import get as get_recipe

    st = start_target_fn or start_target
    ss = start_service_fn or start_service

    datastores = [t for t in targets if get_recipe(t.stack).kind == "service"]
    backends = [t for t in targets
                if get_recipe(t.stack).kind == "build" and t.type != "frontend"]
    frontends = [t for t in targets if t.type == "frontend"]

    urls: dict = {}
    health_paths: dict = {}
    services: dict = {}
    containers: list = []
    failed: list = []

    def _naming(t):
        # Pass the namespacing kwargs only when actually namespacing, so prefix="" callers
        # (DeployPhase, pre-M21 test fakes) see the exact legacy call shapes.
        if not alias_prefix and not container_prefix:
            return {}
        return {"alias": f"{alias_prefix}{t.name}",
                "container_name": f"{container_prefix or 'devagent-preview-' + alias_prefix}{t.name}"}

    # 1. datastores first
    for t in datastores:
        cname = ss(t, network=network, **_naming(t))
        if cname:
            services[t.name] = cname
            containers.append(cname)
        else:
            failed.append(t.name)

    # 2. backends — seed extra_env, then the target's own declared datastore wiring wins
    svc_recipe = {t.name: get_recipe(t.stack).service for t in datastores}
    for t in backends:
        env: dict = dict(extra_env or {})
        ds = (t.detail or {}).get("datastore")
        if ds and ds in svc_recipe:
            conn_env = (t.detail or {}).get("conn_env", "DATABASE_URL")
            env[conn_env] = svc_recipe[ds].conn_url_template.format(
                host=f"{alias_prefix}{ds}", port=svc_recipe[ds].port)
        url = st(workdir, t, network=network, env=env or None, **_naming(t))
        if url:
            urls[t.name] = url
            recipe = get_recipe(t.stack)
            health_paths[t.name] = recipe.boot.health_path if recipe.boot else "/"
            containers.append(f"devagent-preview-{alias_prefix}{t.name}")
        else:
            failed.append(t.name)

    # 3. frontends — wired via dist/config.json to the first backend (or the caller's apiBase)
    backend_url = next(iter(urls.values()), "") if urls else (frontend_api_base or "")
    for t in frontends:
        if backend_url:
            dist_dir = Path(workdir) / t.name / "dist"
            if dist_dir.exists():
                (dist_dir / "config.json").write_text(
                    json.dumps({"apiBase": backend_url}), encoding="utf-8")
            else:
                print(f"warning: frontend {t.name} has no dist/ — skipping config.json "
                      "(apiBase wiring lost)", file=sys.stderr)
        url = st(workdir, t, **_naming(t))
        if url:
            urls[t.name] = url
            health_paths[t.name] = "/"
            containers.append(f"devagent-preview-{alias_prefix}{t.name}")
        else:
            failed.append(t.name)

    primary_url = ""
    for t in frontends + backends:
        if t.name in urls:
            primary_url = urls[t.name]
            break

    return WiredTargets(urls=urls, health_paths=health_paths, services=services,
                        containers=containers, failed=failed, primary_url=primary_url)
