"""BuildVerifier — deterministic re-verification of the executor's output.

The build Executor's BuildResult.success is the executor's CLAIM and is not trusted. This
re-runs the build FROM SOURCE in a clean disposable container per target:

    rm -rf dist && <recipe.build_cmd>

- `rm -rf dist` first → a hand-written/stale dist/ can't pass; the bundle must be produced
  by an actual build.
- `--frozen-lockfile` → the lockfile must exist and match package.json (the pinned-deps
  check); an unpinned or drifted dep tree fails here.

After all targets rebuild green it runs the kind-dispatched acceptance checks ONCE for the
whole project (acceptance_runner.py reads scope.json and dispatches per-target — M6).

Verify needs NO API key (it runs no model), so — unlike SdkExecutor — nothing secret is
passed to the container. It DOES need npm-registry egress for `pnpm install`; when an egress
network is provided (the default for `--build`) the rebuild + acceptance run on the
`--internal` allowlist network behind the proxy (see egress.py), else the default bridge.
Reuses the M2 image (node + pnpm + Playwright/chromium).
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import egress
from . import recipes

DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
ACCEPTANCE_RUNNER = Path(__file__).parent / "acceptance_runner.py"
# rm the prior bundle so only a real build can make it reappear; build_cmd comes from recipe.
REBUILD_CMD = "rm -rf dist && {build_cmd}"


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
    build_ok: bool          # all target rebuild commands exited 0
    dist_present: bool      # all target artifact globs present after rebuild
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
        """The single authoritative signal: all targets rebuild from source AND pass acceptance."""
        return self.build_ok and self.dist_present and self.checks_pass


class BuildVerifier:
    def __init__(self, image: str = DEFAULT_IMAGE, timeout: int = 600,
                 network: str | None = None, proxy_url: str | None = None,
                 runner=None):
        self.image = image
        self.timeout = timeout
        self.network = network        # egress-allowlist network (None = default bridge)
        self.proxy_url = proxy_url
        self.runner = runner if runner is not None else subprocess.run

    def verify(self, req: VerifyRequest) -> VerifyReport:
        out = Path(req.workdir).resolve()
        scope_path = out / ".devagent" / "scope.json"
        scope = json.loads(scope_path.read_text())
        targets = scope["targets"]

        t0 = time.monotonic()
        all_logs: list[str] = []
        all_build_ok = True
        all_dist_present = True
        last_exit_code = 0

        for target in targets:
            name = target["name"]
            recipe = recipes.get(target["stack"])
            build_cmd = REBUILD_CMD.format(build_cmd=recipe.build_cmd)
            argv = [
                "docker", "run", "--rm",
                *egress.docker_flags(self.network, self.proxy_url),
                "-e", "HOME=/home/node",
                "--user", "1000:1000",
                "-v", f"{out}:/out",
                "-w", f"/out/{name}",
                self.image,
                "sh", "-c", build_cmd,
            ]
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return VerifyReport(
                    build_ok=False, dist_present=False, exit_code=124,
                    wall_clock_s=time.monotonic() - t0,
                    error=f"rebuild timed out after {self.timeout}s",
                )
            log = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
            if log:
                all_logs.append(f"[{name}] {log}")
            if proc.returncode != 0:
                all_build_ok = False
                last_exit_code = proc.returncode
                all_logs.append(f"[{name}] build failed (exit {proc.returncode})")
            # Check artifact glob under the target's subdir
            target_dir = out / name
            artifact_present = bool(list(target_dir.glob(recipe.artifact_glob)))
            if not artifact_present:
                all_dist_present = False
                all_logs.append(f"[{name}] artifact not found: {recipe.artifact_glob}")

        combined_log = "\n".join(all_logs)[-2000:]

        if not all_build_ok or not all_dist_present:
            error = (
                "rebuild from source failed" if not all_build_ok
                else "artifact glob not found for one or more targets"
            )
            return VerifyReport(
                build_ok=all_build_ok, dist_present=all_dist_present,
                exit_code=last_exit_code if not all_build_ok else 1,
                log_tail=combined_log,
                wall_clock_s=time.monotonic() - t0,
                error=error,
            )

        # All targets green; run acceptance ONCE for the whole project.
        checks = self._acceptance(out)
        failed = [f"{c.kind} {c.route}: {c.detail}" for c in checks if not c.ok]
        if failed:
            combined_log = (combined_log + "\nACCEPTANCE FAILURES:\n" + "\n".join(failed))[-2000:]
        return VerifyReport(
            build_ok=True, dist_present=True, exit_code=0, log_tail=combined_log,
            wall_clock_s=time.monotonic() - t0, checks=checks,
        )

    def _acceptance(self, out: Path) -> list[CheckResult]:
        argv = [
            "docker", "run", "--rm",
            # Acceptance is localhost-only, but run it behind the same allowlist anyway —
            # a built app that phones home during the checks must not reach the internet.
            *egress.docker_flags(self.network, self.proxy_url),
            "-e", "HOME=/home/node",
            "--user", "1000:1000",
            "-v", f"{out}:/out",
            "-v", f"{ACCEPTANCE_RUNNER}:/acceptance.py:ro",
            "-w", "/out",
            self.image,
            "python3", "/acceptance.py",
        ]
        try:
            self.runner(argv, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return [CheckResult("_runner", None, False, f"acceptance timed out after {self.timeout}s")]
        acc = out / ".devagent" / "acceptance.json"
        if not acc.is_file():
            return [CheckResult("_runner", None, False, "no acceptance.json produced")]
        data = json.loads(acc.read_text())
        return [CheckResult(c.get("kind"), c.get("route"), bool(c.get("ok")), c.get("detail", ""))
                for c in data.get("checks", [])]
