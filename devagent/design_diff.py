"""M25 — mechanical design diff. An update re-architects the system (prior design + change
request -> new full design); THIS module decides what that means operationally: which
services rebuild, and whether the datastore keeps its data. A pure computation over the two
SystemDesigns — never an LLM emission (an LLM "changed" flag could contradict the specs it
ships next to; a diff cannot).

Rules (spec 2026-07-13):
- A service rebuilds when it is new, any of its own fields changed, or a contract it
  provides OR consumes changed kind/spec.
- schema_changed: any db_schema contract added, removed, or spec-changed — the signal that
  decides data fate (preserve the datastore volume iff unchanged).
- A renamed service (same id, new name) is a NEW service (rebuild), never an in-place edit:
  dirs/containers/volumes key on `name`, so the prior out/ is orphaned, not edited."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .schema import SystemDesign
from .tree import topo_order


def load_design(run_dir) -> SystemDesign:
    """Reload a prior run's gated design (<run_dir>/design.json). Validators re-run on
    load, so a hand-edited or corrupt design fails HERE, not mid-build."""
    return SystemDesign.model_validate_json((Path(run_dir) / "design.json").read_text())


@dataclass(frozen=True)
class DesignDiff:
    changed_ids: list          # topo-ordered (new design) service ids to rebuild
    schema_changed: bool       # a db_schema contract changed -> the update resets data
    renamed: list = field(default_factory=list)   # ids kept but renamed (⊆ changed_ids)


def _contract_key(c) -> str:
    return f"{c.kind}:{json.dumps(c.spec, sort_keys=True)}"


def diff_designs(prior: SystemDesign, new: SystemDesign) -> DesignDiff:
    prior_svc = {s.id: s for s in prior.services}
    prior_ct = {c.id: c for c in prior.contracts}
    new_ct = {c.id: c for c in new.contracts}

    changed_contracts = {
        cid for cid, c in new_ct.items()
        if cid not in prior_ct or _contract_key(prior_ct[cid]) != _contract_key(c)}
    # A REMOVED contract can't implicate a consumer (nothing in the new design may consume
    # it — SystemDesign validates that), but a removed db_schema still resets data below.

    changed, renamed = set(), []
    for svc in new.services:
        old = prior_svc.get(svc.id)
        if old is None:
            changed.add(svc.id)
            continue
        if old.name != svc.name:
            renamed.append(svc.id)
            changed.add(svc.id)
            continue
        if ((old.kind, old.stack, old.prd_slice,
             sorted(old.depends_on), sorted(old.provides), sorted(old.consumes))
                != (svc.kind, svc.stack, svc.prd_slice,
                    sorted(svc.depends_on), sorted(svc.provides), sorted(svc.consumes))):
            changed.add(svc.id)
            continue
        if changed_contracts & (set(svc.provides) | set(svc.consumes)):
            changed.add(svc.id)

    schema_changed = (
        any(new_ct[cid].kind == "db_schema" for cid in changed_contracts)
        or any(c.kind == "db_schema"
               and (c.id not in new_ct or new_ct[c.id].kind != "db_schema")
               for c in prior.contracts))

    return DesignDiff(
        changed_ids=[sid for sid in topo_order(new) if sid in changed],
        schema_changed=schema_changed, renamed=renamed)
