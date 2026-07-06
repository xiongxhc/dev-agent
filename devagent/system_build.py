"""M20 — system-build wiring. Runs Architect -> per-service builds (M15 TreeOrchestrator) ->
multi-container bring-up -> cross-service E2E (M17). The two side-effecting pieces (build one
service; bring the services up) are injected callables so the orchestration is unit-testable
without Docker/tokens; the defaults do the real thing. Nothing in M14-M17 is modified."""

import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .schema import AcceptanceCheck, ArtifactSpec, ProjectScope
from .tree import NodeResult, SUCCEEDED, FAILED, topo_order
from . import recipes

# egress.ensure() is an unlocked check-then-act on the shared devagent-proxy container;
# serialize it across concurrent per-service build worker threads.
_egress_lock = threading.Lock()


def scope_for_node(node, design) -> ProjectScope:
    """The mechanical ProjectScope for one service node — the SystemDesign is the ONLY scope
    authority (one-flow, 2026-07-03). No per-service ScopePhase LLM call: live runs showed the
    sub-run's scope model re-inventing targets (its own duplicate db) and acceptance checks
    that contradicted the frozen contract (object vs array root for GET /polls), leaving no
    response shape a correct build could satisfy.

    Targets: the node itself, plus one service-kind target per design-level datastore
    dependency (named exactly as the design names it, so in-container acceptance boots the
    same store bring-up wires). Checks: derived from the node's PROVIDED contracts
    (contract_utils.derive_checks) + a persistence check when a datastore backs the node;
    a node providing nothing checkable (a frontend) gets a root route_status. Auth: when a
    provided contract declares login/register endpoints, the verify harness's AuthFlow is
    synthesized from them (auth_flow_from_contract) and protected checks run authenticated;
    without a derivable flow, auth-needing checks are dropped (unverifiable mechanically)."""
    from .contract_utils import auth_flow_from_contract, derive_checks, derive_persistence_check
    from .schema import AuthFlow

    by_id = {s.id: s for s in design.services}
    svc_deps = [by_id[d] for d in node.depends_on
                if d in by_id and recipes.get(by_id[d].stack).kind == "service"]
    targets = [ArtifactSpec(type=dep.kind, stack=dep.stack, name=dep.name)
               for dep in svc_deps]

    provided = [c for c in design.contracts if c.id in node.provides]
    flow = next((f for f in (auth_flow_from_contract(c) for c in provided) if f), None)
    checks: list[dict] = []
    for contract in provided:
        checks.extend(derive_checks(contract))
    if svc_deps:
        for contract in provided:
            p = derive_persistence_check(contract)
            if p:
                checks.append(p)
                break
    if flow is None:
        checks = [c for c in checks if not c.get("auth")]
    if not checks:
        checks = [{"kind": "route_status", "route": "/", "expected_status": 200}]

    detail = {"description": node.prd_slice}
    if svc_deps:
        detail["datastore"] = svc_deps[0].name
        detail["conn_env"] = "DATABASE_URL"
    targets.append(ArtifactSpec(type=node.kind, stack=node.stack, name=node.name,
                                detail=detail, auth=AuthFlow(**flow) if flow else None,
                                acceptance_checks=[AcceptanceCheck(**c) for c in checks]))
    return ProjectScope(title=f"{design.title} — {node.name}", targets=targets)


def make_run_node(run_dir, budget, ledger, build_service=None):
    """Return a run_node(node, design) -> NodeResult for M15's TreeOrchestrator. Each service
    is built into <run_dir>/services/<name>/ via
    `build_service(node, design, svc_dir, budget, ledger)` (default: the real pipeline
    sub-run), sharing the one `budget`. A build_service crash becomes a FAILED node."""
    run_dir = Path(run_dir)
    bs = build_service if build_service is not None else _real_build_service

    def run_node(node, design):
        svc_dir = run_dir / "services" / node.name
        svc_dir.mkdir(parents=True, exist_ok=True)
        (svc_dir / "prd.md").write_text(node.prd_slice)
        if recipes.get(node.stack).kind == "service":
            # Datastore-style nodes have no buildable artifact — bring-up starts them straight
            # from the recipe image. ArchitectGate already guarantees the stack is registered.
            return NodeResult(node.id, SUCCEEDED,
                              "service node: started from recipe image at bring-up")
        try:
            status = bs(node, design, str(svc_dir), budget, ledger)
        except Exception as e:  # a sub-run crash is a node failure, not a system-build crash
            return NodeResult(node.id, FAILED, repr(e))
        return NodeResult(node.id, SUCCEEDED if status == "succeeded" else FAILED, str(status))

    return run_node


def _real_build_service(node, design, svc_dir, budget, ledger) -> str:
    """Build one service through the existing plan->build->verify pipeline (deploy-less:
    system bring-up starts the real containers), sharing the system budget and injecting the
    node's contracts (M16 seam) — both the ones it consumes and the ones it provides (the
    producer must implement its own frozen interface; live-run finding 2026-07-03: without it
    the api invented routes/fields its consumers didn't call). The sub-run's scope is FROZEN
    (scope_for_node): derived from the design, never re-decided by a second LLM call.
    Returns the run's terminal status string."""
    from .cli import build_pipeline_phases
    from .config import Config
    from .contract_utils import contracts_for_node
    from .orchestrator import Orchestrator
    from .sandbox import NullSandbox
    from .verifier import BuildVerifier
    from . import egress
    from .executor_sdk import SdkExecutor
    from .managed_executor import ManagedExecutor

    cfg = Config.load()
    out_dir = Path(svc_dir) / "out"
    network = proxy = None
    if cfg.egress:
        with _egress_lock:
            network, proxy = egress.ensure()
    executor = (ManagedExecutor() if cfg.executor == "managed"
                else SdkExecutor(network=network, proxy_url=proxy, model=cfg.build_model))
    verifier = BuildVerifier(network=network, proxy_url=proxy)
    consumed = tuple(c.spec for c in contracts_for_node(node, design))
    provided = tuple(c.spec for c in design.contracts if c.id in node.provides)
    phases, gates = build_pipeline_phases(
        str(Path(svc_dir) / "prd.md"), build=True, deploy=False, out_dir=out_dir,
        run_id=f"svc-{node.name}", executor=executor, verifier=verifier,
        consumed_contracts=consumed, provided_contracts=provided,
        scope=scope_for_node(node, design))
    orch = Orchestrator(phases=phases, gates=gates, budget=budget, ledger=ledger,
                        sandbox=NullSandbox())
    return orch.run()


def make_bring_up(run_dir, *, ensure_network=None, start_service=None, start_target=None,
                  probe=None):
    """Return bring_up(design) -> (base_urls, teardown). Starts each service on a fresh
    per-run docker network and collects {service_id -> base_url}. Build-kind nodes are driven
    from the sub-run's persisted out/.devagent/scope.json — the targets ACTUALLY built (scope
    names are LLM-chosen, not node.name) — wired via deploy.wire_targets exactly as a
    single-run DeployPhase would, namespaced per node. Service-kind nodes start straight from
    their recipe image. A node enters base_urls only once EVERY started URL answers at its
    health path (probe = DeployGate's poll; start_target returns before the app listens, and
    an E2E fired at t=0 sees nothing but connection resets — live-run finding, 2026-07-03).
    teardown() removes every started container (+ its data volume) and the network. Deploy
    helpers and the probe are injectable for tests."""
    from . import deploy
    en = ensure_network or deploy.ensure_network
    ss = start_service or deploy.start_service
    st = start_target or deploy.start_target
    pr = probe or deploy._probe
    run_dir = Path(run_dir)

    def bring_up(design):
        net = f"devagent-sys-{run_dir.name}"              # per-run: never shared across builds
        en(net)
        base_urls = {}
        started = []  # container names this call actually started, for teardown

        def teardown():
            # Containers run --restart unless-stopped, so `docker network rm` alone leaves
            # them attached and fails silently. Remove containers first (plus their per-run
            # -data volumes, which would otherwise accumulate run over run), then the network.
            # check=False and try/except so this never raises (tests call it without docker).
            for container in started:
                try:
                    subprocess.run(["docker", "rm", "-f", container],
                                   capture_output=True, check=False)
                    subprocess.run(["docker", "volume", "rm", f"{container}-data"],
                                   capture_output=True, check=False)
                except Exception:
                    pass
            try:
                subprocess.run(["docker", "network", "rm", net],
                               capture_output=True, check=False)
            except Exception:
                pass

        by_id = {s.id: s for s in design.services}
        try:
            for sid in topo_order(design):
                node = by_id[sid]
                if recipes.get(node.stack).kind == "service":
                    # Run-scoped container name (alias stays node.name for conn URLs): a fixed
                    # name like devagent-preview-db is shared with single-run previews and
                    # every other system run — each caller docker-rm-f's the other's live
                    # container (live-run finding, 2026-07-03).
                    container = ss(node, network=net, alias=node.name,
                                   container_name=f"{net}-{node.name}")
                    if container:
                        started.append(container)
                    continue

                out_dir = run_dir / "services" / node.name / "out"
                scope_path = out_dir / ".devagent" / "scope.json"
                if not scope_path.is_file():
                    continue        # nothing built -> absent from base_urls -> E2E fails its steps
                targets = [SimpleNamespace(name=t["name"], type=t["type"], stack=t["stack"],
                                           detail=t.get("detail") or {})
                           for t in json.loads(scope_path.read_text())["targets"]]
                # A sub-scope's service-kind targets are design-level dependencies (the frozen
                # scope names them from the design) — they exist so in-container acceptance can
                # boot a store, and are started ABOVE as design nodes. Wiring them again here
                # would run a second datastore with ambiguous conn wiring.
                targets = [t for t in targets
                           if not (recipes.is_registered(t.stack)
                                   and recipes.get(t.stack).kind == "service")]

                deps = [by_id[d] for d in node.depends_on]
                extra_env = None
                svc_deps = [d for d in deps if recipes.get(d.stack).kind == "service"]
                if svc_deps:
                    dsvc = recipes.get(svc_deps[0].stack).service
                    extra_env = {"DATABASE_URL": dsvc.conn_url_template.format(
                        host=svc_deps[0].name, port=dsvc.port)}
                api_base = next((base_urls[d.id] for d in deps if d.id in base_urls), None)

                wired = deploy.wire_targets(targets, str(out_dir), network=net,
                                            alias_prefix=f"{node.name}-",
                                            container_prefix=f"{net}-{node.name}-",
                                            extra_env=extra_env, frontend_api_base=api_base,
                                            start_target_fn=st, start_service_fn=ss)
                started.extend(wired.containers)
                if not wired.primary_url:
                    continue
                if not all(pr(url.rstrip("/") + wired.health_paths.get(name, "/"))
                           for name, url in wired.urls.items()):
                    continue    # never became healthy -> absent from base_urls -> E2E names it
                base_urls[sid] = wired.primary_url
        except Exception:
            # A mid-loop crash must not leak already-started containers or the per-run network.
            teardown()
            raise

        return base_urls, teardown

    return bring_up


@dataclass
class SystemReport:
    title: str
    node_results: dict           # node_id -> NodeResult
    build_ok: bool
    integration: object          # IntegrationReport | None
    status: str                  # design_failed | build_failed | integration_failed | succeeded
    urls: dict = field(default_factory=dict)   # service_id -> preview base_url (on success)


def build_system(prd_path, *, budget, ledger, run_node, bring_up,
                 architect=None, integration_runner=None, run_dir=None) -> SystemReport:
    """Deterministic system-build orchestration. `run_node`, `bring_up`, `architect`, and
    `integration_runner` are injected (defaults do the real thing) so this is unit-testable
    without Docker/tokens. `run_dir` (optional) persists the gated SystemDesign to
    <run_dir>/design.json — the run's authoritative design record (contracts + integration
    checks), without which a failed live run can't be diagnosed against what the architect
    actually asked for.

    One-flow (2026-07-03): integration checks are DERIVED from the design's contracts
    (contract_utils.derive_integration_checks) — the same assertions each producer already
    passed in-container — falling back to the architect's hand-written integration_checks
    only when nothing is derivable. On success the brought-up system is KEPT as the preview
    (urls in the report), exactly like a single-run deploy; teardown happens only on failure.
    The final ledger event carries the true post-integration status (tree_build_end is the
    per-service build verdict only)."""
    from .tree import TreeOrchestrator
    from .contract_utils import derive_integration_checks
    from .integration import IntegrationRunner
    from .phase_gates import ArchitectGate, IntegrationGate
    from .phases.base import PhaseContext, PhaseResult

    def _finish(report: SystemReport) -> SystemReport:
        if ledger is not None:
            ledger.append({"event": "system_build_end", "status": report.status})
        return report

    # Architect -> SystemDesign, gated.
    if architect is None:
        from .phases.architect import ArchitectPhase
        ctx = PhaseContext(sandbox=None, budget=budget, ledger=ledger)
        res = ArchitectPhase(prd_path).run(ctx)
        design = res.output_artifact
        if ledger is not None:   # architect cost was invisible in the ledger (review finding)
            ledger.append({"event": "phase", "phase": "architect", "exit": res.exit_code,
                           "output": res.output, "meta": res.meta})
        if not ArchitectGate().check(res).ok:
            return _finish(SystemReport(getattr(design, "title", "?"), {}, False, None,
                                        "design_failed"))
    else:
        design = architect(prd_path)

    if run_dir is not None:
        try:
            Path(run_dir).mkdir(parents=True, exist_ok=True)
            (Path(run_dir) / "design.json").write_text(design.model_dump_json(indent=2))
        except OSError:
            pass    # observability only — never fail the build over it

    # SystemDesign only guarantees unique ids; per-service dirs/containers key on `node.name`.
    names = [s.name for s in design.services]
    if len(names) != len(set(names)):
        dups = sorted({n for n in names if names.count(n) > 1})
        if ledger is not None:
            ledger.append({"event": "design_duplicate_names", "names": dups})
        return _finish(SystemReport(design.title, {}, False, None, "design_failed"))

    # Build every service (M15).
    sysres = TreeOrchestrator(run_node=run_node, ledger=ledger).run(design)
    build_ok = sysres.status == "succeeded"
    if not build_ok:
        return _finish(SystemReport(design.title, sysres.results, False, None, "build_failed"))

    # Bring up + cross-service E2E (M17). Success keeps the system running as the preview.
    base_urls, teardown = bring_up(design)
    try:
        checks = derive_integration_checks(design) or design.integration_checks
        runner = integration_runner or IntegrationRunner().run
        report = runner(checks, base_urls)
        ok = IntegrationGate().check(
            PhaseResult(name="integration", exit_code=0, output_artifact=report)).ok
    except Exception:
        teardown()
        raise
    if not ok:
        teardown()
        return _finish(SystemReport(design.title, sysres.results, True, report,
                                    "integration_failed"))
    if ledger is not None:
        ledger.append({"event": "system_deploy", "urls": dict(base_urls)})
    return _finish(SystemReport(design.title, sysres.results, True, report, "succeeded",
                                urls=dict(base_urls)))
