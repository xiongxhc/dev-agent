"""The Executor seam — the single swappable component that makes the A/B fair.

Everything else in the pipeline (intake, spec, plan, verify, deploy, gates, eval) is
SHARED; only the build EXECUTION ENGINE differs. M2 ships `SdkExecutor` (Agent SDK +
subagents in the sandbox). M4 adds `ManagedExecutor` (Claude Managed Agents) behind this
identical interface.

Fairness rule: `BuildResult.success` is the executor's own CLAIM and is NOT trusted —
the build/verify gates re-check the produced repo deterministically.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import recipes
from .schema import Plan, ProjectScope


@dataclass(frozen=True)
class BuildRequest:
    scope: ProjectScope   # frozen — identical bytes go to both A/B arms
    plan: Plan            # frozen task list with disjoint file ownership
    workdir: str          # host-visible out/ dir the built repo must land in
    run_id: str
    budget: Any = None    # shared Budget instance (token/wall-clock/retry ceilings)
    repair_context: str | None = None  # verify diagnostics for a repair pass (None = fresh build)


@dataclass
class BuildResult:
    repo_path: str               # built source on disk — verify runs on THIS
    success: bool                # executor's claim — NOT trusted; gates re-check
    tokens_in: int = 0           # reported total INCLUDING cache create+read (cost transparency)
    tokens_out: int = 0
    cache_read_tokens: int = 0   # cheap (~0.1x) cached portion of tokens_in; excluded from budget
    wall_clock_s: float = 0.0
    transcript_path: str | None = None  # full trace -> failure-transparency metric
    tool_calls: list[dict] = field(default_factory=list)  # observability
    cost_usd: float | None = None
    error: str | None = None

    @property
    def budget_tokens(self) -> int:
        """Tokens that count against the runaway ceiling: everything EXCEPT cache-read.
        Cache-read (~0.1x cost) dominates an agentic build's raw token_in but is not a runaway
        signal — counting it at full weight tripped the ceiling on a legitimate build (the
        persistence live run hit 1.42M token_in of which 1.33M was cache-read, $1.80 total)."""
        return self.tokens_in + self.tokens_out - self.cache_read_tokens


class Executor(Protocol):
    def build(self, req: BuildRequest) -> BuildResult: ...


def enrich_scope(scope) -> dict:
    """Bake recipe-derived fields into a JSON-serializable scope dict that the in-container
    build prompt + acceptance runner consume (they stay registry-free)."""
    targets = []
    for t in scope.targets:
        r = recipes.get(t.stack)
        boot = ({"cmd": list(r.boot.cmd), "port": r.boot.port, "health_path": r.boot.health_path}
                if r.boot is not None else None)
        # static targets serve the dir holding the built bundle (parent of artifact_glob, e.g. "dist")
        static_dir = None if r.boot is not None else (str(Path(r.artifact_glob).parent) or ".")
        targets.append({
            "name": t.name, "type": t.type, "stack": t.stack,
            "detail": t.detail,
            "auth": t.auth.model_dump() if t.auth else None,
            "actors": [a.model_dump() for a in t.actors],
            "acceptance_checks": [c.model_dump() for c in t.acceptance_checks],
            "kind": r.kind,
            "_scaffold_hint": r.scaffold_hint,
            "build_cmd": r.build_cmd,
            "artifact_glob": r.artifact_glob,
            "_boot": boot,
            "_static_dir": static_dir,
        })
    return {"title": scope.title, "targets": targets}
