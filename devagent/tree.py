"""M15 — the tree scheduler. Takes a SystemDesign (M14) and runs each service as an
isolated sub-run in dependency order, independent siblings concurrently, blocking a
subtree when a producer fails. "Build one service" is the injected `run_node` seam —
the scheduler itself is pure logic (no Docker/tokens), exactly like the executor seam.
Real per-service scope->plan->build->verify and git-repo accretion (M7) live behind it."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .schema import ServiceNode, SystemDesign

# Node terminal statuses.
SUCCEEDED = "succeeded"
FAILED = "failed"
BLOCKED = "blocked"  # a dependency failed/blocked; this node was never run


@dataclass
class NodeResult:
    node_id: str
    status: str          # SUCCEEDED | FAILED | BLOCKED
    detail: str = ""


@dataclass
class SystemBuildResult:
    results: dict[str, NodeResult]   # node_id -> NodeResult
    status: str                      # SUCCEEDED iff every node succeeded, else FAILED
    order: list[str]                 # topological order used


def topo_order(design: SystemDesign) -> list[str]:
    """Service ids in dependency order (a node follows all its depends_on). Kahn's
    algorithm; ties broken by declaration order for determinism. The SystemDesign
    validator already guarantees the graph is acyclic, so this always terminates."""
    decl = [s.id for s in design.services]
    deps = {s.id: set(s.depends_on) for s in design.services}
    done: list[str] = []
    placed: set[str] = set()
    while len(done) < len(decl):
        progress = False
        for sid in decl:                       # declaration order = deterministic tiebreak
            if sid not in placed and deps[sid] <= placed:
                done.append(sid)
                placed.add(sid)
                progress = True
        if not progress:
            unplaced = [s for s in decl if s not in placed]
            if not unplaced:  # all unique ids placed but decl longer => duplicate ids
                dups = sorted({s for s in decl if decl.count(s) > 1})
                raise ValueError(f"topo_order: duplicate service ids {dups}")
            raise ValueError(
                f"topo_order: unresolved or cyclic dependencies among {sorted(set(unplaced))}")
    return done


class TreeOrchestrator:
    """Schedules a SystemDesign's services in dependency order — independent siblings
    concurrently — delegating each service build to the injected `run_node`. A node runs
    only when all its depends_on SUCCEEDED; a node with any FAILED/BLOCKED dependency is
    recorded BLOCKED and never run (design §5)."""

    def __init__(self, run_node, concurrency=None, ledger=None):
        self.run_node = run_node          # callable(node, design) -> NodeResult
        self.concurrency = concurrency
        self.ledger = ledger

    @staticmethod
    def _cap_from_env() -> int:
        raw = os.getenv("DEVAGENT_BUILD_CONCURRENCY")
        try:
            return int(raw) if raw else 3
        except ValueError:
            return 3  # malformed env must never crash the build

    def _resolve_cap(self) -> int:
        # `is not None` (not `or`) so an explicit concurrency=0 is honored, not swallowed
        return self.concurrency if self.concurrency is not None else self._cap_from_env()

    def _log(self, event):
        if self.ledger is not None:
            self.ledger.append(event)

    def run(self, design: SystemDesign) -> SystemBuildResult:
        order = topo_order(design)
        by_id = {s.id: s for s in design.services}
        deps = {s.id: list(s.depends_on) for s in design.services}
        results: dict[str, NodeResult] = {}
        self._log({"event": "system_build_start", "order": order})
        remaining = set(order)
        cap = self._resolve_cap()
        while remaining:
            # A node is ready when every dependency already has a result.
            ready = [sid for sid in order
                     if sid in remaining and all(d in results for d in deps[sid])]
            # Split ready nodes: blocked (a dep failed/blocked) vs runnable.
            blocked = [sid for sid in ready
                       if any(results[d].status != SUCCEEDED for d in deps[sid])]
            runnable = [sid for sid in ready if sid not in blocked]
            for sid in blocked:
                bad = [d for d in deps[sid] if results[d].status != SUCCEEDED]
                results[sid] = NodeResult(sid, BLOCKED, f"dependency not satisfied: {bad}")
                self._log({"event": "node", "node": sid, "status": BLOCKED,
                           "detail": results[sid].detail})
                remaining.discard(sid)
            if runnable:
                width = min(len(runnable), max(1, cap))
                with ThreadPoolExecutor(max_workers=width) as pool:
                    futures = {pool.submit(self.run_node, by_id[sid], design): sid
                               for sid in runnable}
                    for fut, sid in futures.items():
                        try:
                            nr = fut.result()
                        except Exception as e:  # a run_node crash is a node failure, not a run crash
                            nr = NodeResult(sid, FAILED, repr(e))
                        if not isinstance(nr, NodeResult):
                            nr = NodeResult(sid, FAILED,
                                            f"run_node returned {type(nr).__name__}, not NodeResult")
                        results[sid] = nr
                        self._log({"event": "node", "node": sid,
                                   "status": nr.status, "detail": nr.detail})
                        remaining.discard(sid)
        status = (SUCCEEDED if results and all(r.status == SUCCEEDED for r in results.values())
                  else FAILED)
        self._log({"event": "system_build_end", "status": status})
        return SystemBuildResult(results=results, status=status, order=order)
