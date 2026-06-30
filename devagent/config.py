"""Run configuration. Defaults are sane for M1; every field is overridable via a
DEVAGENT_* environment variable so the daemon can be configured without code edits."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    image: str = "devagent-sandbox:m1"
    runs_dir: Path = Path("runs")
    max_tokens: int = 1_000_000  # runaway ceiling; counts cache-read tokens too, so multi-target
                                 # builds need headroom — a 2-target build is ~320k+ (the M6 live
                                 # run aborted at the old 200k). Override via DEVAGENT_MAX_TOKENS.
    max_seconds: float = 1800.0
    max_retries: int = 3
    max_cost_usd: float = 10.0   # hard $ ceiling per run — kills a doomed repair loop. Raise for
                                 # prod via DEVAGENT_MAX_COST_USD (0/empty disables the cap).
    egress: bool = True  # route --build containers through the egress allowlist (DEVAGENT_EGRESS=0 to disable)
    executor: str = "sdk"  # the A/B build arm: "sdk" (Agent SDK, our Docker) | "managed" (Managed Agents)

    @classmethod
    def load(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            image=env.get("DEVAGENT_IMAGE", cls.image),
            runs_dir=Path(env.get("DEVAGENT_RUNS_DIR", str(cls.runs_dir))),
            max_tokens=int(env.get("DEVAGENT_MAX_TOKENS", cls.max_tokens)),
            max_seconds=float(env.get("DEVAGENT_MAX_SECONDS", cls.max_seconds)),
            max_retries=int(env.get("DEVAGENT_MAX_RETRIES", cls.max_retries)),
            max_cost_usd=(float(env["DEVAGENT_MAX_COST_USD"]) or None
                          if env.get("DEVAGENT_MAX_COST_USD") else cls.max_cost_usd),
            egress=env.get("DEVAGENT_EGRESS", "1").lower() not in ("0", "false", "no"),
            executor=env.get("DEVAGENT_EXECUTOR", cls.executor).lower(),
        )
