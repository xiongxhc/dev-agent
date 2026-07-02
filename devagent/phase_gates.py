"""Phase gates — deterministic, per-artifact checks that say whether a brain phase's
output is ready for the next phase. Each reads result.output_artifact (a ProjectScope/Plan
or a VerifyReport/BuildResult) and result.exit_code; pydantic already guarantees field
types, so these assert the "ready for the next phase" semantic guarantees on top."""

import json
from dataclasses import dataclass
from pathlib import Path

from . import recipes
from .executor import BuildResult
from .gates import GateResult
from .schema import Plan, ProjectScope, SystemDesign
from .verifier import VerifyReport


def _precheck(result, expected_type) -> GateResult | None:
    """Shared failure modes: the phase errored, or the artifact is missing/wrong type.
    Returns a failing GateResult, or None if the artifact is present and well-typed."""
    if result.exit_code != 0:
        return GateResult(False, f"{result.name} exited {result.exit_code}: {result.output!r}")
    art = result.output_artifact
    if art is None:
        return GateResult(False, f"{result.name} produced no output_artifact")
    if not isinstance(art, expected_type):
        return GateResult(
            False, f"expected {expected_type.__name__}, got {type(art).__name__}"
        )
    return None


@dataclass
class PlanGate:
    """Passes iff the Plan is buildable: >=1 task, every task owns >=1 file, and file
    ownership is pairwise disjoint (defense-in-depth — the Plan validator enforces this
    too, but a gate failure here makes a collision explicit)."""

    name: str = "plan_disjoint_covers"

    def check(self, result) -> GateResult:
        fail = _precheck(result, Plan)
        if fail:
            return fail
        plan: Plan = result.output_artifact
        if not plan.tasks:
            return GateResult(False, "plan has no tasks")
        seen: dict[str, str] = {}
        for t in plan.tasks:
            if not t.owned_files:
                return GateResult(False, f"task {t.id!r} owns no files")
            for f in t.owned_files:
                if f in seen:
                    return GateResult(
                        False,
                        f"file {f!r} owned by both task {seen[f]!r} and {t.id!r}",
                    )
                seen[f] = t.id
        return GateResult(True)


@dataclass
class BuildGate:
    """Passes iff the build produced every target's artifact on disk.

    When <repo_path>/.devagent/scope.json exists (written by enrich_scope via the executor),
    checks each target: Path(repo_path)/<target["name"]> must contain at least one file
    matching <target["artifact_glob"]> (via pathlib.Path.glob). All targets must pass.
    Fails with a clear message naming the first missing target.

    LEGACY fallback: if scope.json is absent, falls back to checking dist/index.html at
    the top of repo_path (pre-M6 / non-scope callers). This keeps existing callers working.

    Deterministic — the executor's BuildResult.success CLAIM is deliberately ignored so
    a lying or crashed executor is caught. (M2 next increment upgrades this to a full
    `pnpm build` re-run + pinned-deps check.)"""

    name: str = "build_produced_bundle"

    def check(self, result) -> GateResult:
        fail = _precheck(result, BuildResult)
        if fail:
            return fail
        build: BuildResult = result.output_artifact
        repo = Path(build.repo_path)
        scope_file = repo / ".devagent" / "scope.json"
        if scope_file.is_file():
            scope = json.loads(scope_file.read_text())
            for t in scope.get("targets", []):
                if t.get("kind") == "service":
                    continue                       # datastores have no buildable artifact
                target_dir = repo / t["name"]
                matches = list(target_dir.glob(t["artifact_glob"]))
                if not matches:
                    return GateResult(
                        False,
                        f"target {t['name']!r}: no artifact matching {t['artifact_glob']!r}"
                        f" under {str(target_dir)!r}",
                    )
            return GateResult(True)
        # Legacy fallback: no scope.json — check flat dist/index.html
        index = repo / "dist" / "index.html"
        if not index.is_file():
            return GateResult(False, f"no dist/index.html under {build.repo_path!r}")
        return GateResult(True)


@dataclass
class VerifyGate:
    """Passes iff the deterministic rebuild-from-source succeeded AND produced the bundle.

    Reads the VerifyReport (rm -rf dist && pnpm install --frozen-lockfile && pnpm build):
    the build command must have exited 0 and dist/index.html must exist afterwards. This
    is the trusted re-check that BuildResult.success is not."""

    name: str = "rebuilds_and_passes_acceptance"

    def check(self, result) -> GateResult:
        fail = _precheck(result, VerifyReport)
        if fail:
            return fail
        rep: VerifyReport = result.output_artifact
        if not rep.build_ok:
            return GateResult(False, f"rebuild from source failed (exit {rep.exit_code})")
        if not rep.dist_present:
            return GateResult(False, "one or more target artifacts missing after rebuild")
        if not rep.checks:
            return GateResult(False, "no acceptance checks ran")
        failed = [f"{c.kind} {c.route}" for c in rep.checks if not c.ok]
        if failed:
            return GateResult(False, f"acceptance checks failed: {', '.join(failed)}")
        return GateResult(True)


@dataclass
class ScopeGate:
    """Passes iff the ProjectScope is confirmed AND every target is buildable: no pending
    clarifications, ≥1 target, each target's stack is a registered recipe whose type matches,
    ≥1 acceptance check per target, and every check kind is supported by that recipe."""

    name: str = "scope_buildable"

    def check(self, result) -> GateResult:
        fail = _precheck(result, ProjectScope)
        if fail:
            return fail
        scope: ProjectScope = result.output_artifact
        if scope.clarifications:
            return GateResult(False, f"scope has pending clarifications: {scope.clarifications}")
        if not scope.targets:
            return GateResult(False, "scope has no targets")
        service_names = {t.name for t in scope.targets
                         if recipes.is_registered(t.stack) and recipes.get(t.stack).kind == "service"}
        for t in scope.targets:
            if not recipes.is_registered(t.stack):
                return GateResult(False, f"target {t.name!r}: no recipe yet for stack {t.stack!r}")
            r = recipes.get(t.stack)
            if r.type != t.type:
                return GateResult(False,
                    f"target {t.name!r}: recipe {t.stack!r} is type {r.type!r}, not {t.type!r}")
            # A datastore reference must name a real service target in this scope.
            ds = t.detail.get("datastore") if isinstance(t.detail, dict) else None
            if ds and ds not in service_names:
                return GateResult(False,
                    f"target {t.name!r}: detail.datastore {ds!r} is not a service target in this scope")
            # Service targets carry no acceptance checks of their own; build targets must.
            if r.kind != "service" and not t.acceptance_checks:
                return GateResult(False, f"target {t.name!r}: no acceptance_checks")
            for chk in t.acceptance_checks:
                if chk.kind not in r.supported_checks:
                    return GateResult(False,
                        f"target {t.name!r}: check kind {chk.kind!r} unsupported by {t.stack!r}")
        return GateResult(True)


@dataclass
class ArchitectGate:
    """Passes iff the SystemDesign is buildable as a tree: >=1 service, an acyclic dependency
    graph, and every consumed contract provided by a declared dependency. The schema validator
    already guarantees these (so generate_structured raises on a malformed emit); this gate is
    the defense-in-depth re-check at the phase boundary, exactly like PlanGate."""

    name: str = "system_design_buildable"

    def check(self, result) -> GateResult:
        fail = _precheck(result, SystemDesign)
        if fail:
            return fail
        design: SystemDesign = result.output_artifact
        if not design.services:
            return GateResult(False, "system design has no services")
        ids = {s.id for s in design.services}
        for s in design.services:
            for d in s.depends_on:
                if d not in ids:
                    return GateResult(
                        False, f"service {s.id!r} depends_on unknown service {d!r}")
        graph = {s.id: list(s.depends_on) for s in design.services}
        color = {sid: 0 for sid in graph}

        def visit(n):
            color[n] = 1
            for m in graph[n]:
                if color[m] == 1:
                    return m
                if color[m] == 0:
                    cyc = visit(m)
                    if cyc:
                        return cyc
            color[n] = 2
            return None

        for sid in graph:
            if color[sid] == 0:
                cyc = visit(sid)
                if cyc:
                    return GateResult(False, f"dependency cycle through service {cyc!r}")
        provides_by = {s.id: set(s.provides) for s in design.services}
        for s in design.services:
            deps_provide = set().union(*(provides_by[d] for d in s.depends_on)) \
                if s.depends_on else set()
            for cid in s.consumes:
                if cid not in deps_provide:
                    return GateResult(
                        False,
                        f"service {s.id!r} consumes {cid!r} not provided by any dependency")
        return GateResult(True)
