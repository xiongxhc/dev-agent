"""The Executor seam — the single swappable component that makes the A/B fair.

Everything else in the pipeline (intake, spec, plan, verify, deploy, gates, eval) is
SHARED; only the build EXECUTION ENGINE differs. M2 ships `SdkExecutor` (Agent SDK +
subagents in the sandbox). M4 adds `ManagedExecutor` (Claude Managed Agents) behind this
identical interface.

Fairness rule: `BuildResult.success` is the executor's own CLAIM and is NOT trusted —
the build/verify gates re-check the produced repo deterministically.
"""

import re
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
    consumed_contracts: tuple = ()     # M21: contract-spec dicts this service consumes (M16 seam)
    provided_contracts: tuple = ()     # contract-spec dicts this service must IMPLEMENT (producer side)


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


def enrich_scope(scope, consumed_by_target: dict | None = None,
                 provided_by_target: dict | None = None) -> dict:
    """Bake recipe-derived fields into a JSON-serializable scope dict that the in-container
    build prompt + acceptance runner consume (they stay registry-free). `consumed_by_target`
    (M16) maps a target name -> list of consumed contract-spec dicts, injected read-only into
    that target's build prompt. `provided_by_target` maps a target name -> the contract-spec
    dicts that target must IMPLEMENT — the producer side of the same frozen contracts (without
    it a producer invents its own routes/shapes while its consumers build against the contract;
    live-run finding, 2026-07-03). Absent/empty means no cross-service contracts (default)."""
    consumed_by_target = consumed_by_target or {}
    provided_by_target = provided_by_target or {}
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
            "acceptance_checks": _contract_conformed_checks(
                [c.model_dump() for c in t.acceptance_checks],
                provided_by_target.get(t.name, [])),
            "kind": r.kind,
            "_scaffold_hint": r.scaffold_hint,
            "build_cmd": r.build_cmd,
            "artifact_glob": r.artifact_glob,
            "_boot": boot,
            "_static_dir": static_dir,
            "_consumed_contracts": consumed_by_target.get(t.name, []),
            "_provided_contracts": provided_by_target.get(t.name, []),
        })
    return {"title": scope.title, "targets": targets}


def _contract_conformed_checks(checks: list, provided: list) -> list:
    """Drop LLM-emitted route_status checks the provided contract contradicts. route_status
    probes with GET; when the frozen openapi declares a matching path GET-less, a
    success-expecting probe (expected_status < 400) can never pass against a CORRECT build —
    the repair loop then burns its repairs on an unsatisfiable check (live-run finding,
    2026-07-03). The contract is the authority, not the scope model's check. Failure-asserting
    probes (>= 400) and api_json checks (which carry their own method) are untouched.

    Contract-free rule (Feishu live run, same day): a route_status expecting 201 Created is
    dropped unconditionally — the probe is a GET, and a GET never creates; the scope model
    writes these against POST register/create routes despite its prompt forbidding it."""
    checks = [c for c in checks
              if not (c.get("kind") == "route_status" and c.get("expected_status") == 201)]
    getless = []   # regexes for contract paths that do NOT answer GET
    for spec in provided:
        for path, methods in (spec.get("paths") or {}).items():
            if isinstance(methods, dict) and "get" not in {str(m).lower() for m in methods}:
                getless.append(re.compile(
                    "^" + re.sub(r"\{[^/}]+\}", "[^/]+", path.rstrip("/")) + "/?$"))
    if not getless:
        return checks
    return [c for c in checks
            if not (c.get("kind") == "route_status" and c.get("expected_status", 200) < 400
                    and any(rx.match(str(c.get("route", ""))) for rx in getless))]


def broadcast_consumed(scope, contracts) -> dict | None:
    """M21: a sub-run builds exactly ONE service, so its contract specs apply to every target
    of its scope (same broadcast for consumed and provided sides). Targets are LLM-named (not
    pinned to the design's node name), so broadcasting per-target is the only stable keying
    for enrich_scope's consumed_by_target/provided_by_target."""
    if not contracts:
        return None
    return {t.name: list(contracts) for t in scope.targets}
