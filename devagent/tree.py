"""M15 — the tree scheduler. Takes a SystemDesign (M14) and runs each service as an
isolated sub-run in dependency order, independent siblings concurrently, blocking a
subtree when a producer fails. "Build one service" is the injected `run_node` seam —
the scheduler itself is pure logic (no Docker/tokens), exactly like the executor seam.
Real per-service scope->plan->build->verify and git-repo accretion (M7) live behind it."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
    results: dict         # node_id -> NodeResult
    status: str           # SUCCEEDED iff every node succeeded, else FAILED
    order: list           # topological order used


def topo_order(design: SystemDesign) -> list[str]:
    """Service ids in dependency order (a node follows all its depends_on). Kahn's
    algorithm; ties broken by declaration order for determinism. The SystemDesign
    validator already guarantees the graph is acyclic, so this always terminates."""
    decl = [s.id for s in design.services]
    deps = {s.id: set(s.depends_on) for s in design.services}
    done: list[str] = []
    placed: set[str] = set()
    while len(done) < len(decl):
        for sid in decl:                       # declaration order = deterministic tiebreak
            if sid not in placed and deps[sid] <= placed:
                done.append(sid)
                placed.add(sid)
    return done
