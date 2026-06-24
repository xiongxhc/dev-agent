"""SdkExecutor — the self-built A/B arm. Launches the disposable M2 container and runs
the Claude Agent SDK INSIDE it (via sdk_runner.py) to build the app per the frozen
Spec+Plan, writing to the host-mounted /out. Returns a BuildResult.

The API key is passed by NAME (`-e ANTHROPIC_API_KEY`) so the value flows via the host
process env, never into the `docker run` argv (which the ledger records) — per the M1
security review. When an egress network is provided (the default for `--build`), the
container runs on the `--internal` allowlist network behind the proxy (see egress.py);
otherwise it uses the default bridge.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from . import egress
from .executor import BuildRequest, BuildResult

RUNNER = Path(__file__).parent / "sdk_runner.py"
DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")


class SdkExecutor:
    def __init__(self, image: str = DEFAULT_IMAGE, runner: Path = RUNNER,
                 max_turns: int = 40, timeout: int = 1200,
                 api_key_env: str = "ANTHROPIC_API_KEY",
                 network: str | None = None, proxy_url: str | None = None):
        self.image = image
        self.runner = Path(runner)
        self.max_turns = max_turns
        self.timeout = timeout
        self.api_key_env = api_key_env
        self.network = network        # egress-allowlist network (None = default bridge)
        self.proxy_url = proxy_url

    def build(self, req: BuildRequest) -> BuildResult:
        out = Path(req.workdir).resolve()
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        (dev / "spec.json").write_text(req.spec.model_dump_json())
        (dev / "plan.json").write_text(req.plan.model_dump_json())
        repair = dev / "repair.txt"
        if req.repair_context:
            # A repair pass: hand the runner the prior verify diagnostics to fix.
            repair.write_text(req.repair_context)
        elif repair.exists():
            repair.unlink()  # stale diagnostics from a prior pass must not leak in

        argv = [
            "docker", "run", "--rm",
            *egress.docker_flags(self.network, self.proxy_url),  # [] when egress disabled
            "-e", self.api_key_env,        # value via env, NOT argv (no secret in argv)
            "-e", "HOME=/home/node",
            "--user", "1000:1000",
            "-v", f"{out}:/out",
            "-v", f"{self.runner}:/runner.py:ro",
            self.image,
            "python3", "/runner.py", "--max-turns", str(self.max_turns),
        ]
        t0 = time.monotonic()
        try:
            subprocess.run(argv, capture_output=True, text=True,
                           timeout=self.timeout, env={**os.environ})
        except subprocess.TimeoutExpired:
            return BuildResult(repo_path=str(out), success=False,
                               wall_clock_s=time.monotonic() - t0,
                               error=f"build timed out after {self.timeout}s")
        wall = time.monotonic() - t0

        result_path = dev / "result.json"
        r = json.loads(result_path.read_text()) if result_path.exists() else {}
        built = (out / "dist" / "index.html").exists()
        return BuildResult(
            repo_path=str(out),
            success=bool(r.get("ok_stream")) and built,  # CLAIM — the build gate re-checks
            tokens_in=int(r.get("tokens_in") or 0),
            tokens_out=int(r.get("tokens_out") or 0),
            wall_clock_s=wall,
            cost_usd=r.get("cost_usd"),
            transcript_path=str(result_path) if result_path.exists() else None,
            error=r.get("error") or (None if built else "no dist/index.html produced"),
        )
