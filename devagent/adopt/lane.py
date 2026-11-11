"""The adopt lane — survey → map → assemble → prove, into a synthetic run dir.

The output is shaped exactly like a `build-system` run, which is the whole point:
`update-system <adopt-run-dir> <change.md>` then works with no new plumbing, because it loads
`design.json` and `services/<name>/` and does not care who wrote them.

    <out>/
      survey.json      what the repo is, and every recipe gap
      design.json      the gated SystemDesign — what update-system consumes
      services/<n>/out a COPY of that service's source
      ledger.jsonl     append-only event trail
      report.html      what a human reviews before any of this is treated as truth

**The source repo is never written to.** Source is copied out; nothing is copied back, and a
run dir inside a source repo is refused. Changing adopted code is a separate step that consumes
what this produces.

A failed stage degrades honestly — a service becomes unproven or unmappable and the report says
so. Only an unreadable repo aborts the run.
"""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..ledger import Ledger
from . import assemble as _assemble
from .prove import PROVEN, UNMAPPABLE, UNPROVEN, prove
from .report import write_report
from .survey import survey_repo, survey_repos

#: Never adopted as source: vendored dependencies and prior build output. Checked against the
#: top-level name as well as passed to copytree — `ignore_patterns` filters a directory's
#: CONTENTS, so copytree(node_modules, ...) would happily copy the whole thing.
_EXCLUDE_NAMES = frozenset({".git", "node_modules", "target", "dist", "build", ".venv",
                            "__pycache__", ".idea", ".mvn", ".gradle", "out"})
_COPY_EXCLUDE = shutil.ignore_patterns(*_EXCLUDE_NAMES, "*.log")


@dataclass
class AdoptResult:
    run_dir: str
    title: str
    survey: object
    design: object | None = None
    verdicts: list = field(default_factory=list)
    externals: list = field(default_factory=list)   # consumed but owned by something else
    status: str = "adopted"          # adopted | survey_only | gate_failed
    detail: str = ""

    @property
    def counts(self) -> dict:
        out = {PROVEN: 0, UNPROVEN: 0, UNMAPPABLE: 0}
        for v in self.verdicts:
            out[v.status] = out.get(v.status, 0) + 1
        return out


def _is_reactor_module(service) -> bool:
    """A Maven module that is part of a multi-module build, as opposed to a single-module repo."""
    return service.kind == "maven-module" and service.path != "."


def _copy_source(service, dest: Path) -> None:
    """Copy one service's source out of the repo. Read-only with respect to the repo: the
    ignore list keeps vendored dependencies and prior build output from being adopted as if
    they were source.

    A symlink is copied as a link at every level and never followed: a link can dangle, point
    back at its own directory, or lead out of the repo, and following it would crash the copy,
    recurse without end, or pull an unrelated tree into the run dir."""
    repo = Path(service.repo)
    # A Maven module cannot build alone: it inherits from the parent POM, depends on sibling
    # modules, and needs the repo's settings.xml. So the REACTOR is the build unit — copy the
    # reactor root and let the recipe build `-pl <module> -am`, the way a multi-module project
    # is built in CI.
    src = repo if (service.path == "." or _is_reactor_module(service)) else repo / service.path
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in _EXCLUDE_NAMES:
            continue
        target = dest / item.name
        if not item.is_symlink() and item.is_dir():
            shutil.copytree(item, target, symlinks=True, ignore=_COPY_EXCLUDE,
                            dirs_exist_ok=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


def _within(path: Path, root: Path) -> bool:
    """True when `path` is `root` or lies under it. Directories are compared on disk, not as
    strings: on a case-insensitive filesystem two spellings name one directory, and `resolve()`
    does not normalise case."""
    if not root.exists():
        return False
    resolved = path.resolve()
    return any(p.exists() and os.path.samefile(p, root) for p in (resolved, *resolved.parents))


def check_run_dir(repos, out: Path) -> None:
    """Adopt describes a repo; it never writes into one. A run dir under a source repo would
    write into it, and so would a source repo under the run dir, where the copies go.

    A run dir that already holds files is refused too: copying over a previous run's tree would
    write through the links it copied, or fail on them. Nothing is deleted to make room."""
    for repo in repos:
        if _within(out, repo):
            raise ValueError(f"the run dir {out} is inside the source repo {repo} — adopt never "
                             f"writes into the repo it describes; pass --out somewhere else")
        if _within(repo, out):
            raise ValueError(f"the source repo {repo} is inside the run dir {out} — adopt "
                             f"writes its copies under the run dir and would write into the "
                             f"repo it describes; pass --out somewhere outside the repo")
    if out.is_dir() and any(out.iterdir()):
        raise ValueError(f"the run dir {out} already holds a run — adopt never reuses or cleans "
                         f"one; pass a new --out")


def adopt(repo_path, out_dir, *, map_fn=None, prover=prove, title=None,
          registry=None, network=None, proxy_url=None, ledger=None) -> AdoptResult:
    """Run the lane. `repo_path` is one repo or several that form ONE system.

    `map_fn(repo, service) -> Fragment | None` is the agentic stage, injected so the lane is
    testable end to end without tokens; `None` from it (or no map_fn at all) means the service is
    described from survey evidence and marked NOT MAPPED.

    `prover` receives `modules=`, each service's module path inside its copied source: the
    service's `path` for a Maven reactor module, `.` for everything else, whose copied source
    IS the module."""
    paths = [repo_path] if isinstance(repo_path, (str, Path)) else list(repo_path)
    repos = [Path(p).resolve() for p in paths]
    out = Path(out_dir)
    check_run_dir(repos, out)
    # Survey before anything is created: a survey that raises leaves no run dir behind.
    survey = survey_repos(repos, registry) if len(repos) > 1 else survey_repo(repos[0], registry)
    out.mkdir(parents=True, exist_ok=True)
    ledger = ledger if ledger is not None else Ledger(out)
    ledger.append({"event": "adopt_start", "repos": [str(r) for r in repos]})
    (out / "survey.json").write_text(survey.to_json())
    ledger.append({"event": "adopt_survey", "services": [s.name for s in survey.services],
                   "libraries": [s.name for s in survey.libraries], "gaps": survey.gaps})

    result = AdoptResult(run_dir=str(out), title=title or "", survey=survey)
    if not survey.services:
        result.status, result.detail = "survey_only", "no service roots found"
        ledger.append({"event": "adopt_end", "status": result.status, "detail": result.detail})
        write_report(result)
        return result

    # Map. Read-only per service; a failure degrades that service, never the run.
    fragments = []
    for svc in survey.services:
        frag = None
        if map_fn is not None:
            try:
                frag = map_fn(Path(svc.repo), svc)
            except Exception as e:  # noqa: BLE001 — one service failing to map is a verdict
                ledger.append({"event": "adopt_map_failed", "service": svc.name,
                               "detail": f"{type(e).__name__}: {e}"})
        if frag is not None:
            fragments.append(frag)
        ledger.append({"event": "adopt_mapped", "service": svc.name, "mapped": frag is not None})

    # Assemble + the two gates.
    externals: list = []
    try:
        design = _assemble.compose(survey, fragments, title=title, externals=externals)
    except Exception as e:  # noqa: BLE001 — includes GateFailure and pydantic wiring errors
        result.status, result.detail = "gate_failed", f"{type(e).__name__}: {e}"
        ledger.append({"event": "adopt_end", "status": result.status, "detail": result.detail})
        write_report(result)
        return result
    result.design, result.title, result.externals = design, design.title, externals
    if externals:
        # A finding, not a fault: what this system depends on and does not own is exactly what a
        # reviewer needs to see before treating the adoption as a description of the system.
        ledger.append({"event": "adopt_external_dependencies",
                       "contracts": [e["contract"] for e in externals]})
    (out / "design.json").write_text(design.model_dump_json(indent=2))
    ledger.append({"event": "adopt_design", "services": [s.id for s in design.services],
                   "contracts": [c.id for c in design.contracts]})

    # Source out of the repo, into the shape update-system expects.
    for svc in survey.services:
        _copy_source(svc, out / "services" / svc.name / "out")
    ledger.append({"event": "adopt_sources", "count": len(survey.services)})

    modules = {svc.name: svc.path if _is_reactor_module(svc) else "."
               for svc in survey.services}
    result.verdicts = prover(design, out, modules=modules, registry=registry, network=network,
                             proxy_url=proxy_url)
    for v in result.verdicts:
        # The build log is what makes an `unproven` verdict actionable; keep it beside the source
        # it came from.
        log_path = None
        if getattr(v, "log_tail", ""):
            log_path = out / "services" / v.service / "build.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(v.log_tail)
        ledger.append({"event": "adopt_proved", "service": v.service, "status": v.status,
                       "detail": v.detail,
                       **({"log": str(log_path.relative_to(out))} if log_path else {})})
    ledger.append({"event": "adopt_end", "status": result.status, **result.counts})
    write_report(result)
    return result
