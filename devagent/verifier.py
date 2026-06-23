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

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
ACCEPTANCE_RUNNER = Path(__file__).parent / "acceptance_runner.py"
# rm the prior bundle so only a real build can make it reappear; --frozen-lockfile pins deps.
REBUILD_CMD = "rm -rf dist && pnpm install --frozen-lockfile && pnpm build"


@dataclass(frozen=True)
class VerifyRequest:
    workdir: str   # host out/ dir holding the executor's produced repo
    run_id: str


@dataclass
class CheckResult:
    kind: str
    route: str | None
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    build_ok: bool          # the rebuild command exited 0
    dist_present: bool      # dist/index.html exists after the rebuild
    exit_code: int
    log_tail: str = ""      # tail of stdout+stderr — diagnostics for the repair loop
    wall_clock_s: float = 0.0
    error: str | None = None
    checks: list[CheckResult] = field(default_factory=list)  # acceptance results (kind-dispatched)

    @property
    def checks_pass(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def ok(self) -> bool:
        """The single authoritative signal: rebuilds from source AND passes acceptance."""
        return self.build_ok and self.dist_present and self.checks_pass


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
        log = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
        if proc.returncode != 0 or not dist:
            return VerifyReport(
                build_ok=proc.returncode == 0, dist_present=dist, exit_code=proc.returncode,
                log_tail=log, wall_clock_s=time.monotonic() - t0,
                error="rebuild from source failed" if proc.returncode != 0 else "no dist/index.html",
            )
        # Build is real; now run the kind-dispatched acceptance checks against it.
        checks = self._acceptance(out)
        failed = [f"{c.kind} {c.route}: {c.detail}" for c in checks if not c.ok]
        if failed:  # surface acceptance failures as repair diagnostics
            log = (log + "\nACCEPTANCE FAILURES:\n" + "\n".join(failed))[-2000:]
        return VerifyReport(
            build_ok=True, dist_present=True, exit_code=0, log_tail=log,
            wall_clock_s=time.monotonic() - t0, checks=checks,
        )

    def _acceptance(self, out: Path) -> list[CheckResult]:
        argv = [
            "docker", "run", "--rm",
            "-e", "HOME=/home/node",
            "--user", "1000:1000",
            "-v", f"{out}:/out",
            "-v", f"{ACCEPTANCE_RUNNER}:/acceptance.py:ro",
            "-w", "/out",
            self.image,
            "python3", "/acceptance.py",
        ]
        try:
            subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return [CheckResult("_runner", None, False, f"acceptance timed out after {self.timeout}s")]
        acc = out / ".devagent" / "acceptance.json"
        if not acc.is_file():
            return [CheckResult("_runner", None, False, "no acceptance.json produced")]
        data = json.loads(acc.read_text())
        return [CheckResult(c.get("kind"), c.get("route"), bool(c.get("ok")), c.get("detail", ""))
                for c in data.get("checks", [])]
