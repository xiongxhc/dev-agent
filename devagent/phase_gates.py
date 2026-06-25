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
from .schema import Plan, ProjectScope
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
        for t in scope.targets:
            if not recipes.is_registered(t.stack):
                return GateResult(False, f"target {t.name!r}: no recipe yet for stack {t.stack!r}")
            r = recipes.get(t.stack)
            if r.type != t.type:
                return GateResult(False,
                    f"target {t.name!r}: recipe {t.stack!r} is type {r.type!r}, not {t.type!r}")
            if not t.acceptance_checks:
                return GateResult(False, f"target {t.name!r}: no acceptance_checks")
            for chk in t.acceptance_checks:
                if chk.kind not in r.supported_checks:
                    return GateResult(False,
                        f"target {t.name!r}: check kind {chk.kind!r} unsupported by {t.stack!r}")
        return GateResult(True)
