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
        "docker", "run", "-d", "--name", CONTAINER,
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


def _wait_service_ready(container: str, ready_cmd, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = subprocess.run(["docker", "exec", container, *ready_cmd],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return True
        time.sleep(1.0)
    return False


def start_service(target, image: str = DEFAULT_IMAGE, network: str | None = None) -> str | None:
    """Start a datastore sibling container for preview; return its container name or None.
    A named volume at the engine's data dir makes the data survive `docker restart`."""
    from .recipes import get as get_recipe

    svc = get_recipe(target.stack).service
    container_name = f"devagent-preview-{target.name}"
    vol = f"{container_name}-data"
    env_flags = []
    for k, v in svc.env:
        env_flags += ["-e", f"{k}={v}"]
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
    argv = ["docker", "run", "-d", "--name", container_name]
    if network:
        argv += ["--network", network, "--network-alias", target.name]
    argv += [*env_flags, "-v", f"{vol}:{svc.volume_path}", svc.image]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    if not _wait_service_ready(container_name, svc.ready_cmd, svc.ready_timeout_s):
        return None
    return container_name


def start_target(out_dir: str, target, image: str = DEFAULT_IMAGE,
                 network: str | None = None, env: dict | None = None) -> str | None:
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
    container_name = f"devagent-preview-{target.name}"

    if boot is not None:
        # --- backend: run the built service detached ---
        host_port = _free_port()
        target_dir = out / target.name
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
        env_flags = []
        for k, v in (env or {}).items():
            env_flags += ["-e", f"{k}={v}"]
        net_flags = ["--network", network, "--network-alias", target.name] if network else []
        argv = [
            "docker", "run", "-d", "--name", container_name,
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
            "docker", "run", "-d", "--name", container_name,
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
