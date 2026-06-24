"""M3 local preview — `deploy → preview URL`. Starts a detached container that serves the
built bundle (/out/dist) via preview_server.py on a host port, and exposes a DeployGate that
proves the URL actually answers HTTP 200.

Unlike SdkExecutor/BuildVerifier this needs no API key — a static server runs no model, so
nothing secret is passed to the container (the docker argv carries no key). Reuses the M2
image (python3 only) and the same `--user 1000:1000`, `:ro`-mount argv style."""

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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


@dataclass
class DeployGate:
    """Passes iff the preview URL answers HTTP 200. A freshly-started container needs a
    moment to bind, so this polls for up to ~10s before failing."""

    name: str = "preview_responds"

    def check(self, result) -> GateResult:
        if result.exit_code != 0:
            return GateResult(False, f"{result.name} exited {result.exit_code}: {result.output!r}")
        art = result.output_artifact
        if art is None:
            return GateResult(False, f"{result.name} produced no output_artifact")
        if not art.url:
            return GateResult(False, "deploy produced no url")
        deadline = time.monotonic() + 10
        last = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(art.url, timeout=2) as resp:
                    if resp.status == 200:
                        return GateResult(True)
                    last = f"status {resp.status}"
            except (urllib.error.URLError, OSError) as e:
                last = str(e)
            time.sleep(0.2)
        return GateResult(False, f"preview did not return 200: {last}")
