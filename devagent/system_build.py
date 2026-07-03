"""M20 — system-build wiring. Runs Architect -> per-service builds (M15 TreeOrchestrator) ->
multi-container bring-up -> cross-service E2E (M17). The two side-effecting pieces (build one
service; bring the services up) are injected callables so the orchestration is unit-testable
without Docker/tokens; the defaults do the real thing. Nothing in M14-M17 is modified."""

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .tree import NodeResult, SUCCEEDED, FAILED, topo_order
from . import recipes

# egress.ensure() is an unlocked check-then-act on the shared devagent-proxy container;
# serialize it across concurrent per-service build worker threads.
_egress_lock = threading.Lock()


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
    """Build one service through the existing scope->plan->build->verify pipeline (deploy-less:
    system bring-up starts the real containers), sharing the system budget and injecting the
    node's contracts (M16 seam) — both the ones it consumes and the ones it provides (the
    producer must implement its own frozen interface; live-run finding 2026-07-03: without it
    the api invented routes/fields its consumers didn't call). Returns the run's terminal
    status string."""
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
        consumed_contracts=consumed, provided_contracts=provided)
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
                    container = ss(node, network=net)      # design-level datastore: alias node.name
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


def build_system(prd_path, *, budget, ledger, run_node, bring_up,
                 architect=None, integration_runner=None, run_dir=None) -> SystemReport:
    """Deterministic system-build orchestration. `run_node`, `bring_up`, `architect`, and
    `integration_runner` are injected (defaults do the real thing) so this is unit-testable
    without Docker/tokens. `run_dir` (optional) persists the gated SystemDesign to
    <run_dir>/design.json — the run's authoritative design record (contracts + integration
    checks), without which a failed live run can't be diagnosed against what the architect
    actually asked for."""
    from .tree import TreeOrchestrator
    from .integration import IntegrationRunner
    from .phase_gates import ArchitectGate, IntegrationGate
    from .phases.base import PhaseContext, PhaseResult

    # Architect -> SystemDesign, gated.
    if architect is None:
        from .phases.architect import ArchitectPhase
        ctx = PhaseContext(sandbox=None, budget=budget, ledger=ledger)
        res = ArchitectPhase(prd_path).run(ctx)
        design = res.output_artifact
        if not ArchitectGate().check(res).ok:
            return SystemReport(getattr(design, "title", "?"), {}, False, None, "design_failed")
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
        return SystemReport(design.title, {}, False, None, "design_failed")

    # Build every service (M15).
    sysres = TreeOrchestrator(run_node=run_node, ledger=ledger).run(design)
    build_ok = sysres.status == "succeeded"
    if not build_ok:
        return SystemReport(design.title, sysres.results, False, None, "build_failed")

    # Bring up + cross-service E2E (M17), teardown always.
    base_urls, teardown = bring_up(design)
    try:
        runner = integration_runner or IntegrationRunner().run
        report = runner(design.integration_checks, base_urls)
        ok = IntegrationGate().check(
            PhaseResult(name="integration", exit_code=0, output_artifact=report)).ok
    finally:
        teardown()
    return SystemReport(design.title, sysres.results, True, report,
                        "succeeded" if ok else "integration_failed")
