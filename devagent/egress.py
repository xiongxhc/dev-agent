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
import sys
from contextlib import contextmanager
from pathlib import Path

from . import hostlock
from .egress_proxy import DEFAULT_ALLOW

PROXY_SCRIPT = Path(__file__).parent / "egress_proxy.py"
NETWORK = "devagent-egress"
PROXY = "devagent-proxy"
PORT = 3128
DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")


def _run(args):
    return subprocess.run(args, capture_output=True, text=True)


def hosts_for_stacks(stacks) -> list[str]:
    """Egress hosts the named recipes declare they need. A recipe's toolchain knows which
    package registries its build contacts; nothing else does. Names that are empty or not a
    registered recipe contribute nothing."""
    from . import recipes as _recipes
    hosts = []
    for stack in stacks:
        if not stack or not _recipes.is_registered(stack):
            continue
        for host in _recipes.get(stack).toolchain.egress_hosts or ():
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def allowlist(extra_hosts=()) -> str:
    """The allowlist the proxy should enforce, as the comma string it parses.

    DEVAGENT_EGRESS_ALLOW replaces the defaults (that is its documented meaning); `extra_hosts`
    are ADDED either way, since a registry the build needs that the proxy blocks is a silent,
    confusing build failure."""
    override = os.getenv("DEVAGENT_EGRESS_ALLOW")
    base = [h.strip() for h in override.split(",") if h.strip()] if override \
        else list(DEFAULT_ALLOW)
    for h in extra_hosts:
        if h and h not in base:
            base.append(h)
    return ",".join(base)


def ensure(image: str = DEFAULT_IMAGE, extra_hosts=()) -> tuple[str, str]:
    """Make sure the internal network + proxy exist and are running. Returns
    (network_name, proxy_url) for callers to attach build/verify containers to.

    One proxy serves every build on the Docker host and is reused across runs, so the allowlist
    it was started with is recorded as a label and compared here. A proxy with exactly this
    caller's allowlist is reused. While nothing else is attached to the egress network, a proxy
    with any other allowlist, wider ones included, is replaced with exactly this caller's, so a
    wider list does not outlive the run that needed it. While another container is attached, a
    proxy that already permits every host this caller needs is reused, and any other raises
    RuntimeError rather than break that build.

    Calls are serialised by a lock under `hostlock.lock_root()`, which is the same for every run
    of this user on this host whatever its DEVAGENT_RUNS_DIR. A lock still held after its
    timeout raises TimeoutError."""
    allow = allowlist(extra_hosts)
    with _ensure_lock():
        return _ensure_locked(image, allow)


def _ensure_locked(image: str, allow: str) -> tuple[str, str]:
    """The body of `ensure`, under the host lock.

    The lock serialises concurrent `ensure` calls, so two runs starting together cannot both see
    "no proxy" and both create one. It does not cover the time after `ensure` returns: a proxy
    already in use is protected by the reuse and refusal rules below, not by the lock."""
    if _run(["docker", "network", "inspect", NETWORK]).returncode != 0:
        _run(["docker", "network", "create", "--internal", NETWORK])
    running = _run(["docker", "inspect", "-f", "{{.State.Running}}", PROXY]).stdout.strip()
    current = _run(["docker", "inspect", "-f",
                    "{{index .Config.Labels \"devagent.allow\"}}", PROXY]).stdout.strip()
    if running == "true" and current != allow and _network_busy():
        if _allow_covers(current, allow):
            # Another build is using a proxy that already permits every host this run needs:
            # share it. Replacing it would break that build.
            return NETWORK, f"http://{PROXY}:{PORT}"
        # A list that lacks hosts this run needs, AND someone else is attached. Refusing beats
        # stealing: the alternative is silently killing another run's build.
        raise RuntimeError(
            "the shared egress proxy is running with a different allowlist and other containers are "
            "attached to its network, so replacing it would break another run mid-build. Wait for "
            "that run, or give both runs the same egress allowlist.")
    if running != "true" or current != allow:
        _run(["docker", "rm", "-f", PROXY])  # dead, stale, or configured for a different allowlist
        up = _run([
            "docker", "run", "-d", "--name", PROXY, "--network", NETWORK,
            "--user", "1000:1000",
            # The proxy reads its allowlist from its OWN env, so it is passed in here. It is not
            # a secret, and inlining makes the proxy's rules visible in `docker ps`.
            "-e", f"DEVAGENT_EGRESS_ALLOW={allow}",
            "--label", f"devagent.allow={allow}",
            "-v", f"{PROXY_SCRIPT}:/proxy.py:ro",
            image, "python3", "/proxy.py",
        ])
        if up.returncode != 0:
            raise RuntimeError(f"egress proxy failed to start: {up.stderr.strip()}")
        # Give the proxy (and ONLY the proxy) upstream internet via the default bridge.
        _run(["docker", "network", "connect", "bridge", PROXY])
    return NETWORK, f"http://{PROXY}:{PORT}"


@contextmanager
def _ensure_lock():
    """Serialise `ensure` across the runs of this user on this host. A lock directory that cannot
    be created must not stop a build, so the lock is then skipped. A lock still held after the
    timeout raises TimeoutError to the caller, because running unlocked would reopen the race.
    An error from the body propagates, and the lock is released."""
    lock = hostlock.exclusive("egress", timeout_s=180.0,
                              on_wait=lambda n: print(
                                  "  -> waiting for another run to finish preparing the shared "
                                  "egress proxy", flush=True))
    try:
        lock.__enter__()
    except TimeoutError:
        raise                       # a held lock must stop us; running unlocked reopens the race
    except OSError:
        yield
        return
    try:
        yield
    except BaseException:
        if not lock.__exit__(*sys.exc_info()):
            raise
    else:
        lock.__exit__(None, None, None)


def _allow_covers(current: str, needed: str) -> bool:
    """Does the running proxy already permit everything this run needs?"""
    have = {h.strip().lower() for h in (current or "").split(",") if h.strip()}
    want = {h.strip().lower() for h in (needed or "").split(",") if h.strip()}
    return bool(have) and want.issubset(have)


def _network_busy() -> bool:
    """Is anything other than the proxy attached to the egress network right now?"""
    out = _run(["docker", "network", "inspect", "-f",
                "{{range .Containers}}{{.Name}} {{end}}", NETWORK]).stdout
    return any(n != PROXY for n in out.split())


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
