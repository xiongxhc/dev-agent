"""The Executor seam — the single swappable component that makes the A/B fair.

Everything else in the pipeline (intake, spec, plan, verify, deploy, gates, eval) is
SHARED; only the build EXECUTION ENGINE differs. M2 ships `SdkExecutor` (Agent SDK +
subagents in the sandbox). M4 adds `ManagedExecutor` (Claude Managed Agents) behind this
identical interface.

Fairness rule: `BuildResult.success` is the executor's own CLAIM and is NOT trusted —
the build/verify gates re-check the produced repo deterministically.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from .schema import Plan, Spec


@dataclass(frozen=True)
class BuildRequest:
    spec: Spec          # frozen — identical bytes go to both A/B arms
    plan: Plan          # frozen task list with disjoint file ownership
    workdir: str        # host-visible out/ dir the built repo must land in
    run_id: str
    budget: Any = None  # shared Budget instance (token/wall-clock/retry ceilings)
    repair_context: str | None = None  # verify diagnostics for a repair pass (None = fresh build)


@dataclass
class BuildResult:
    repo_path: str               # built source on disk — verify runs on THIS
    success: bool                # executor's claim — NOT trusted; gates re-check
    tokens_in: int = 0
    tokens_out: int = 0
    wall_clock_s: float = 0.0
    transcript_path: str | None = None  # full trace -> failure-transparency metric
    tool_calls: list[dict] = field(default_factory=list)  # observability
    cost_usd: float | None = None
    error: str | None = None


class Executor(Protocol):
    def build(self, req: BuildRequest) -> BuildResult: ...
