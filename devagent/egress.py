"""Provisions the egress-allowlist network + proxy for contained build/verify. Idempotent.

Topology (portable — works on Docker Desktop and native Linux alike):
- `devagent-egress` is an --internal Docker network: members have NO direct route out.
- `devagent-proxy` (the egress_proxy in the M2 image) sits on that internal network AND on
  the default bridge, so it alone can reach the internet — and only to allowlisted hosts.
- build/verify containers run on the internal network with HTTPS_PROXY pointed at the proxy,
  so every byte of egress goes through the allowlist.

The proxy is long-lived and reused across runs; `teardown()` removes it.
"""

import os
import subprocess
from pathlib import Path

PROXY_SCRIPT = Path(__file__).parent / "egress_proxy.py"
NETWORK = "devagent-egress"
PROXY = "devagent-proxy"
PORT = 3128
DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")


def _run(args):
    return subprocess.run(args, capture_output=True, text=True)


def ensure(image: str = DEFAULT_IMAGE) -> tuple[str, str]:
    """Make sure the internal network + proxy exist and are running. Returns
    (network_name, proxy_url) for callers to attach build/verify containers to."""
    if _run(["docker", "network", "inspect", NETWORK]).returncode != 0:
        _run(["docker", "network", "create", "--internal", NETWORK])
    running = _run(["docker", "inspect", "-f", "{{.State.Running}}", PROXY]).stdout.strip()
    if running != "true":
        _run(["docker", "rm", "-f", PROXY])  # clear any dead/stale container
        up = _run([
            "docker", "run", "-d", "--name", PROXY, "--network", NETWORK,
            "--user", "1000:1000",
            "-v", f"{PROXY_SCRIPT}:/proxy.py:ro",
            image, "python3", "/proxy.py",
        ])
        if up.returncode != 0:
            raise RuntimeError(f"egress proxy failed to start: {up.stderr.strip()}")
        # Give the proxy (and ONLY the proxy) upstream internet via the default bridge.
        _run(["docker", "network", "connect", "bridge", PROXY])
    return NETWORK, f"http://{PROXY}:{PORT}"


def docker_flags(network: str | None, proxy_url: str | None) -> list[str]:
    """`docker run` flags that put a container on the egress network behind the proxy.
    Empty when network is None (egress disabled) — the caller keeps the default bridge.
    Both upper/lower-case proxy vars are set since toolchain components differ."""
    if not network:
        return []
    return [
        "--network", network,
        "-e", f"HTTPS_PROXY={proxy_url}", "-e", f"https_proxy={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}", "-e", f"http_proxy={proxy_url}",
        "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
    ]


def teardown() -> None:
    _run(["docker", "rm", "-f", PROXY])
    _run(["docker", "network", "rm", NETWORK])
