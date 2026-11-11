"""Assemble — compose a SystemDesign from the survey and the per-service map fragments.

Two deterministic gates sit on top of the composition. The pydantic wiring validators on
SystemDesign already gate structure for free (unique ids, producers exist, depends_on resolves,
acyclic); these two gates cover what those cannot see:

* **Every surveyed service appears.** Without it, "the design validates" is trivially achievable
  by dropping whatever was hard — and a design that quietly omits a service is worse than a
  failed adoption, because the omission is invisible downstream.
* **Every contract a service claims to produce has a producing service.** A declared producer
  that is not in the design is a wiring hole.

What is deliberately NOT a gate failure: a service consuming something this system does not
own. A system that calls LDAP, an OAuth2 provider, an SSO, a logging service or another team's
API does not contain them in its repos, and refusing those would make adoption impossible for any
integrated system. They are recorded as EXTERNAL dependencies instead: a finding for the reviewer,
and exactly the kind of thing an adoption should surface.

`stack` is required by the schema, so an UNMAPPABLE service still carries one: the recipe it
*needs* (for example `maven-spring-boot-jdk9`), not a recipe that exists. That keeps the
service visible in the design, makes the gap self-documenting, and lets `prove` classify it as
unmappable rather than pretending it built.
"""

from dataclasses import dataclass

from ..schema import Contract, ServiceNode, SystemDesign
from .survey import _java_major, slug

#: Survey `kind` -> the ArtifactSpec type the build machinery understands.
_KIND = {"maven-module": "backend", "node-app": "frontend"}


def _node_kind(evidence: dict) -> str:
    """A node app with a recognised UI framework is a frontend; one without is a service.

    A node package with build scripts and no UI framework is a service, not a frontend, and
    calling it a frontend would put it on the wrong build and deploy path. This is still only a
    default: the map stage reads the code and may correct it."""
    return "frontend" if evidence.get("framework") else "backend"


@dataclass(frozen=True)
class Fragment:
    """What the map stage returns for one service: its own words, and what it provides."""
    service: str
    prd_slice: str
    kind: str | None = None
    provides: tuple = ()          # Contract-shaped dicts: id, kind, spec
    consumes: tuple = ()          # contract ids this service calls
    depends_on: tuple = ()        # service names


class GateFailure(Exception):
    """A design that cannot be trusted as an artifact. Raised, not returned, because a failed
    gate must stop the adoption being treated as truth."""


def needed_recipe(service) -> str:
    """The recipe a surveyed service requires — its matched stack, or the one its gap names."""
    if service.stack:
        return service.stack
    ev = service.evidence or {}
    if service.kind == "maven-module":
        major = _java_major(ev.get("java_version"))
        return f"maven-spring-boot-jdk{major}" if major is not None else "maven-spring-boot"
    framework, pm = ev.get("framework"), ev.get("package_manager")
    if framework and pm:
        return f"node-{framework}-{pm}"
    if pm:
        # A node package with a build step but no recognised framework still needs an install +
        # build recipe. Falling back to static-web would name a recipe that EXISTS, so prove
        # would attempt it and report a confusing build failure instead of "unmappable".
        return f"node-{pm}"
    return "static-web"


def contract_id(raw: str) -> str:
    """Normalise a consumed reference to a contract id.

    A mapping session tends to be over-specific: it may report `api:GET /users` and
    `api:POST /orders` as separate entries when they are all the SAME contract. Left alone they
    read as many external dependencies when in fact the service consumes one internal API.
    Splitting on the first ':' or whitespace recovers the contract, which is the granularity
    the design models."""
    text = raw.strip()
    for i, ch in enumerate(text):
        if ch == ":" or ch.isspace():
            return text[:i].strip() or text
    return text


def compose(survey, fragments, *, title: str | None = None, externals=None) -> SystemDesign:
    """Build the SystemDesign. Fragments are keyed by service name; a service with no fragment
    still appears, described from the survey's own evidence, so the gate below can pass
    honestly rather than by omission."""
    by_name = {f.service: f for f in fragments}
    services, contracts = [], []
    # Contracts produced inside this system. Anything consumed but not in here belongs to
    # something we do not own, and SystemDesign models intra-system wiring only.
    produced = {c["id"] for f in fragments for c in f.provides}
    external_seen = {}
    # A mapping session names whatever it sees a dependency on, which includes LIBRARY modules.
    # A service may depend on a library module that the survey correctly excluded from the
    # services — so it is a fact about the build, not an edge in the service DAG. Filtering here
    # keeps the design valid; recording it keeps the fact.
    service_ids = {slug(svc.name) for svc in survey.services}
    library_ids = {slug(lib.name): lib.name for lib in survey.libraries}
    library_deps = {}

    for svc in survey.services:
        frag = by_name.get(svc.name)
        kind = _node_kind(svc.evidence or {}) if svc.kind == "node-app" \
            else _KIND.get(svc.kind, "backend")
        if frag and frag.kind:
            kind = frag.kind          # the mapping session read the code; it outranks a default
        slice_text = frag.prd_slice if frag else _slice_from_evidence(svc)
        declared_deps = [slug(d) for d in (frag.depends_on if frag else ())]
        deps = []
        for dep in declared_deps:
            if dep in service_ids and dep != slug(svc.name):
                deps.append(dep)
            elif dep in library_ids:
                library_deps.setdefault(svc.name, []).append(library_ids[dep])
        wanted = []
        for raw in (frag.consumes if frag else ()):
            cid = contract_id(raw)
            if cid and cid not in wanted:
                wanted.append(cid)
        internal = [cid for cid in wanted if cid in produced]
        for cid in wanted:
            if cid not in produced:
                external_seen.setdefault(cid, []).append(svc.name)
        services.append(ServiceNode(
            id=slug(svc.name), name=svc.name, kind=kind, stack=needed_recipe(svc),
            prd_slice=slice_text,
            depends_on=deps,
            provides=[c["id"] for c in (frag.provides if frag else ())],
            consumes=internal,
        ))
        for c in (frag.provides if frag else ()):
            contracts.append(Contract(id=c["id"], kind=c.get("kind", "openapi"),
                                      producer=slug(svc.name), spec=c.get("spec") or {}))

    # Derive the dependency edges from the contract graph. A mapping session sees ONE service,
    # so it can name the contract it calls but not reliably the sibling that serves it — and
    # SystemDesign requires a consumer to list the producer in depends_on. Inferring the edge
    # here is what "assemble" means; leaving it to the mapper would ask it to guess at names it
    # cannot see.
    producer_of = {c.id: c.producer for c in contracts}
    for node in services:
        implied = {producer_of[cid] for cid in node.consumes
                   if cid in producer_of and producer_of[cid] != node.id}
        for dep in sorted(implied - set(node.depends_on)):
            node.depends_on.append(dep)

    # Gate BEFORE constructing SystemDesign. Its validators would reject the same wiring, but
    # with a pydantic message about an unknown contract — the operator needs to be told that an
    # adoption produced a wiring hole, in those words.
    if externals is not None:
        externals.extend({"contract": cid, "consumed_by": sorted(users)}
                         for cid, users in sorted(external_seen.items()))
        externals.extend({"contract": f"(library) {lib}", "consumed_by": [svc]}
                         for svc, libs in sorted(library_deps.items()) for lib in sorted(libs))
    _gate_wiring(services, contracts)
    design = SystemDesign(
        title=title or f"Adopted: {survey.repo.rstrip('/').split('/')[-1]}",
        services=services, contracts=contracts)
    gate_every_service_present(design, survey)
    return design


def _gate_wiring(services, contracts) -> None:
    """Every contract has a producing service, and every consumed contract is produced."""
    producers = {s.id for s in services}
    orphans = [c.id for c in contracts if c.producer not in producers]
    if orphans:
        raise GateFailure(f"contract(s) {orphans} have no producing service in the design")
    produced = {c.id for c in contracts}
    dangling = sorted({cid for s in services for cid in s.consumes} - produced)
    if dangling:
        # Reaching here means an INTERNAL contract went missing between composition and gating —
        # a consumed id that was produced a moment ago. External dependencies never appear here;
        # they are separated out during composition and recorded as findings.
        raise GateFailure(
            f"service(s) consume contract(s) {dangling} that nothing in this design produces — "
            f"a wiring hole that only surfaces when something calls it")


def _slice_from_evidence(svc) -> str:
    """A description built from facts when no mapping session ran. Deliberately not a guess at
    behaviour: it says what was detected, so nobody mistakes it for a read of the code."""
    ev = svc.evidence or {}
    bits = [f"{svc.kind} at ./{svc.path}"]
    if ev.get("java_version"):
        bits.append(f"java {ev['java_version']}")
    if ev.get("framework"):
        bits.append(str(ev["framework"]))
    if ev.get("package_manager"):
        bits.append(str(ev["package_manager"]))
    if ev.get("entry_points"):
        bits.append("entry " + ", ".join(ev["entry_points"]))
    if ev.get("build_targets"):
        bits.append("targets " + ", ".join(ev["build_targets"]))
    return ("NOT MAPPED — surveyed only: " + "; ".join(bits)
            + ". No mapping session read this code, so nothing here describes its behaviour.")


# ---------------------------------------------------------------- the two new gates

def gate_every_service_present(design: SystemDesign, survey) -> None:
    """Every surveyed service must appear in the design."""
    designed = {s.name for s in design.services}
    missing = [s.name for s in survey.services if s.name not in designed]
    if missing:
        raise GateFailure(
            f"design omits surveyed service(s) {missing} — an adoption that silently drops a "
            f"service is not reviewable, because the omission is invisible downstream")


def gate_every_contract_has_a_producer(design: SystemDesign) -> None:
    """Every contract has a producing service, and every consumed contract is produced.

    A built SystemDesign has already passed the schema's own wiring validators, so this is the
    same check expressed as an adopt gate — useful when re-checking a design loaded from disk."""
    _gate_wiring(design.services, design.contracts)
