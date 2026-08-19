"""DeepSeekExecutor — the third A/B arm. Same contained Agent SDK session as SdkExecutor,
pointed at DeepSeek's official Anthropic-compatible endpoint via ANTHROPIC_BASE_URL, so the
runner, image, and parallel-build path are reused unchanged.

Two deltas from the sdk arm:
- Key plumbing: the host's DEEPSEEK_API_KEY is mapped into ANTHROPIC_API_KEY /
  ANTHROPIC_AUTH_TOKEN in the subprocess env, so the container's by-name `-e` flags pick it
  up and the secret never enters the `docker run` argv (M1 rule, same as the sdk arm).
- Cost: the Agent SDK prices its ResultMessage.total_cost_usd at CLAUDE rates — 10-30x too
  high for DeepSeek — which would misfire the max_cost_usd ceiling. Cost is recomputed here
  from the raw usage breakdown at DeepSeek's published $/MTok; no usage → cost_usd=None
  (honest-unknown; None disables the $ ceiling, so prefer runs that report usage).

Endpoint caveats (api-docs.deepseek.com/guides/anthropic_api, checked 2026-08-15): no image
or document blocks (the build pipeline sends neither) and cache_control is ignored —
DeepSeek's transparent cache still reports cache_read_input_tokens, billed at the hit rate.
"""

import os
from pathlib import Path

from .executor import BuildRequest, BuildResult
from .executor_sdk import SdkExecutor

ANTHROPIC_COMPAT_URL = "https://api.deepseek.com/anthropic"
KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_MODEL = os.getenv("DEVAGENT_DEEPSEEK_MODEL", "deepseek-v4-pro")

# $/MTok (cache-hit input, cache-miss input, output) — published 2026-08-15. Cache WRITES
# have no separate rate, so cache_creation bills as miss. Peak/off-peak splits start
# 2026-08-16; these are the flat rates, revisit when the schedule lands.
PRICES = {
    "deepseek-v4-pro": (0.003625, 0.435, 0.87),
    "deepseek-v4-flash": (0.0028, 0.14, 0.28),
}


class DeepSeekExecutor(SdkExecutor):
    def __init__(self, *, model: str | None = None, **kw):
        model = model or DEFAULT_MODEL
        if model not in PRICES:
            raise ValueError(f"unknown DeepSeek model {model!r} (known: {list(PRICES)})")
        super().__init__(model=model, **kw)

    def build(self, req: BuildRequest) -> BuildResult:
        if not os.getenv(KEY_ENV):
            raise RuntimeError(f"{KEY_ENV} is not set — the deepseek arm needs it")
        return super().build(req)

    def _extra_docker_env(self) -> list[str]:
        # base URL is not a secret: inline so the run is reproducible from the ledger argv.
        return ["-e", f"ANTHROPIC_BASE_URL={ANTHROPIC_COMPAT_URL}",
                "-e", "ANTHROPIC_AUTH_TOKEN"]   # value via env, NOT argv

    def _subprocess_env(self) -> dict:
        key = os.environ[KEY_ENV]
        return {**os.environ, "ANTHROPIC_API_KEY": key, "ANTHROPIC_AUTH_TOKEN": key}

    def _aggregate(self, out: Path, req: BuildRequest, results: dict) -> BuildResult:
        res = super()._aggregate(out, req, results)
        res.cost_usd = self._deepseek_cost(results.values())
        return res

    def _deepseek_cost(self, raws) -> float | None:
        hit, miss, out_rate = PRICES[self.model]
        usages = [r.get("usage") for r in raws if r.get("usage")]
        if not usages:
            return None
        cost = 0.0
        for u in usages:
            def n(k):
                return int(u.get(k) or 0)
            cost += (n("input_tokens") + n("cache_creation_input_tokens")) * miss / 1e6
            cost += n("cache_read_input_tokens") * hit / 1e6
            cost += n("output_tokens") * out_rate / 1e6
        return cost
