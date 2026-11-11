"""Prove — rebuild each mappable service from source and check its artifact appears.

Static analysis says what a repo looks like; only a build says the description is usable. The
verdict per service is deliberately three-valued, because two of the three are useful outcomes
rather than failures:

    proven      the recipe's build_cmd succeeded and artifact_glob exists
    unproven    a recipe matched, the build ran, and it did not produce the artifact
    unmappable  no registered recipe fits — nothing was attempted

A partially-proven adoption is still a reviewable artifact, so a failure here degrades the
service's verdict and never aborts the run. Only an unreadable repo does that.

No boot, no datastores, no acceptance probes. This is a compile check.
"""

import posixpath
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

PROVEN, UNPROVEN, UNMAPPABLE = "proven", "unproven", "unmappable"


@dataclass
class Verdict:
    service: str
    status: str
    detail: str = ""
    log_tail: str = field(default="", repr=False)


def _run_build(image: str, workdir: Path, build_cmd: str, network, proxy_url, timeout: int):
    """The verifier's rebuild shape: `rm -rf dist && <build_cmd>` in a disposable container.

    Reused rather than reimplemented so a proven adoption means the same thing a verified build
    means."""
    from .. import egress
    from ..verifier import REBUILD_CMD

    argv = ["docker", "run", "--rm", *egress.docker_flags(network, proxy_url),
            # HOME inside the writable mount: a toolchain image other than ours has no
            # /home/node, and Maven then falls back to /root and cannot write there.
            "-e", "HOME=/out", "--user", "1000:1000",
            "-v", f"{workdir}:/out", "-w", "/out",
            image, "sh", "-c", REBUILD_CMD.format(build_cmd=build_cmd)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _mount_visible(image: str, source: Path, runner=subprocess.run) -> str | None:
    """Explain why `source` will not be visible inside the build container, or None if it will.

    On a VM-backed Docker (Colima, Docker Desktop, Rancher Desktop) only configured host paths
    are shared, and a `-v` of anything else does NOT fail: the daemon creates an empty directory
    in the VM and mounts that. The build then runs against nothing, and what the operator reads
    is a plausible-looking source failure — `yarn install` reporting "No lockfile found" for a
    repo that has one, or Maven unable to create /out/.m2 as uid 1000 in a root-owned empty
    mount. One cheap probe turns that into an instruction.

    The marker is an entry that is not a link, because `test -e` follows a link and a copied
    link can dangle or lead out of the mount. When every entry is a link, the probe tests for
    the link itself."""
    entries = sorted(source.iterdir())
    if not entries:
        return None                      # nothing to see — the caller reports the empty source
    plain = next((p for p in entries if not p.is_symlink()), None)
    flag, marker = ("-e", plain.name) if plain is not None else ("-L", entries[0].name)
    probe = runner(["docker", "run", "--rm", "-v", f"{source}:/probe:ro", image,
                    "test", flag, f"/probe/{marker}"], capture_output=True, text=True)
    if getattr(probe, "returncode", 1) == 0:
        return None
    return (f"{source} is not shared with the Docker daemon, so the build container sees an "
            f"empty directory and fails for a reason that has nothing to do with the code. Put "
            f"the run dir where the daemon can see it — under your home directory on a default "
            f"Colima/Docker Desktop setup (--out ~/devagent-runs/...) — or add this path to the "
            f"VM's shared folders.")


def _outside(module: str) -> bool:
    """True when `module` does not name a path inside the copied source."""
    path = PurePosixPath(module)
    return path.is_absolute() or ".." in path.parts


def prove_service(node, source: Path, *, module: str = ".", registry=None, network=None,
                  proxy_url=None, timeout: int = 1800, runner=None, mount_check=None) -> Verdict:
    """Rebuild one adopted service. `runner` is injectable so the logic is testable without
    Docker; the default runs the real container.

    `module` is the service's path inside the copied source: `.` when the copy IS the service,
    the module's path when the copy is a Maven reactor. An empty path is `.`.

    The mount probe runs on the real Docker path only. An injected `runner` is not Docker, so
    probing there would test nothing and cost a container per service — pass `mount_check`
    explicitly to exercise it."""
    from .. import recipes

    registry = registry if registry is not None else recipes.REGISTRY
    recipe = registry.get(node.stack)
    if recipe is None:
        return Verdict(node.name, UNMAPPABLE,
                       f"no registered recipe {node.stack!r} — nothing was attempted")
    if not recipe.build_cmd:
        return Verdict(node.name, UNMAPPABLE,
                       f"recipe {node.stack!r} declares no build_cmd (datastore-shaped)")
    if not source.is_dir():
        return Verdict(node.name, UNPROVEN, f"no source at {source}")
    if not module.strip():
        module = "."
    if _outside(module):
        return Verdict(node.name, UNPROVEN,
                       f"module {module!r} lies outside the copied source, so there is nothing "
                       f"there to build — nothing was attempted")

    check = mount_check if mount_check is not None else (
        _mount_visible if runner is None else None)
    if check is not None:
        unshared = check(recipe.toolchain.image, source)
        if unshared:
            return Verdict(node.name, UNPROVEN, unshared)

    # `{module}` lets a recipe express a build that is per-module but must run from the reactor
    # root — `mvn -pl <module> -am package`. A module of a multi-module Maven build inherits from
    # the parent POM and depends on sibling modules, so building inside its directory alone
    # fails on principle, not on configuration. The path is repo text inside a `sh -c` command,
    # so it is quoted.
    build_cmd = recipe.build_cmd.replace("{module}", shlex.quote(module))
    artifact = posixpath.normpath(recipe.artifact_glob.replace("{module}", module))
    run = runner or _run_build
    try:
        res = run(recipe.toolchain.image, source, build_cmd, network, proxy_url, timeout)
    except subprocess.TimeoutExpired:
        return Verdict(node.name, UNPROVEN, f"build exceeded {timeout}s")
    except Exception as e:  # noqa: BLE001 — a broken toolchain is a verdict, not a crash
        return Verdict(node.name, UNPROVEN, f"build could not run: {type(e).__name__}: {e}")

    tail = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or ""))[-4000:]
    if getattr(res, "returncode", 1) != 0:
        return Verdict(node.name, UNPROVEN, f"build exited {res.returncode}", tail)
    if not list(source.glob(artifact)):
        return Verdict(node.name, UNPROVEN,
                       f"build succeeded but {artifact} is absent — the artifact is what makes "
                       f"the build real", tail)
    return Verdict(node.name, PROVEN, f"{artifact} produced by {node.stack}", tail)


def prove(design, run_dir: Path, *, modules=None, **kw) -> list:
    """Prove every service in the design, in order. Returns one Verdict each.

    `modules` maps a service name to its module path inside the copied source; a name it lacks
    is `.`."""
    modules = modules or {}
    return [prove_service(node, Path(run_dir) / "services" / node.name / "out",
                          module=modules.get(node.name, "."), **kw)
            for node in design.services]
