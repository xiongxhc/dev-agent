"""BuildVerifier — deterministic re-verification of the executor's output.

The build Executor's BuildResult.success is the executor's CLAIM and is not trusted. This
re-runs the build FROM SOURCE in a clean disposable container:

    rm -rf dist && pnpm install --frozen-lockfile && pnpm build

- `rm -rf dist` first → a hand-written/stale dist/ can't pass; the bundle must be produced
  by an actual build.
- `--frozen-lockfile` → the lockfile must exist and match package.json (the pinned-deps
  check); an unpinned or drifted dep tree fails here.

Verify needs NO API key (it runs no model), so — unlike SdkExecutor — nothing secret is
passed to the container. It DOES need npm-registry egress for `pnpm install`; like
SdkExecutor's build, that currently uses the default bridge network (egress allowlist is
the same shared TODO). Reuses the existing M2 image (node + pnpm); no Playwright yet.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
# rm the prior bundle so only a real build can make it reappear; --frozen-lockfile pins deps.
REBUILD_CMD = "rm -rf dist && pnpm install --frozen-lockfile && pnpm build"


@dataclass(frozen=True)
class VerifyRequest:
    workdir: str   # host out/ dir holding the executor's produced repo
    run_id: str


@dataclass
class VerifyReport:
    build_ok: bool          # the rebuild command exited 0
    dist_present: bool      # dist/index.html exists after the rebuild
    exit_code: int
    log_tail: str = ""      # tail of stdout+stderr — diagnostics for the repair loop
    wall_clock_s: float = 0.0
    error: str | None = None


class BuildVerifier:
    def __init__(self, image: str = DEFAULT_IMAGE, timeout: int = 600):
        self.image = image
        self.timeout = timeout

    def verify(self, req: VerifyRequest) -> VerifyReport:
        out = Path(req.workdir).resolve()
        argv = [
            "docker", "run", "--rm",
            "-e", "HOME=/home/node",
            "--user", "1000:1000",
            "-v", f"{out}:/out",
            "-w", "/out",
            self.image,
            "sh", "-c", REBUILD_CMD,
        ]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return VerifyReport(build_ok=False, dist_present=False, exit_code=124,
                                wall_clock_s=time.monotonic() - t0,
                                error=f"rebuild timed out after {self.timeout}s")
        dist = (out / "dist" / "index.html").is_file()
        return VerifyReport(
            build_ok=proc.returncode == 0,
            dist_present=dist,
            exit_code=proc.returncode,
            log_tail=((proc.stdout or "") + (proc.stderr or ""))[-2000:],
            wall_clock_s=time.monotonic() - t0,
            error=None if proc.returncode == 0 else "rebuild from source failed",
        )
