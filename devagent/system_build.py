"""M20 — system-build wiring. Runs Architect -> per-service builds (M15 TreeOrchestrator) ->
multi-container bring-up -> cross-service E2E (M17). The two side-effecting pieces (build one
service; bring the services up) are injected callables so the orchestration is unit-testable
without Docker/tokens; the defaults do the real thing. Nothing in M14-M17 is modified."""

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .tree import NodeResult, SUCCEEDED, FAILED, topo_order

# egress.ensure() is an unlocked check-then-act on the shared devagent-proxy container;
# serialize it across concurrent per-service build worker threads.
_egress_lock = threading.Lock()


def make_run_node(run_dir, budget, ledger, build_service=None):
    """Return a run_node(node, design) -> NodeResult for M15's TreeOrchestrator. Each service
    is built into <run_dir>/services/<name>/ via `build_service` (default: the real pipeline
    sub-run), sharing the one `budget`. A build_service crash becomes a FAILED node."""
    run_dir = Path(run_dir)
    bs = build_service if build_service is not None else _real_build_service

    def run_node(node, design):
        svc_dir = run_dir / "services" / node.name
        svc_dir.mkdir(parents=True, exist_ok=True)
        (svc_dir / "prd.md").write_text(node.prd_slice)
        try:
            status = bs(node, str(svc_dir), budget, ledger)
        except Exception as e:  # a sub-run crash is a node failure, not a system-build crash
            return NodeResult(node.id, FAILED, repr(e))
        return NodeResult(node.id, SUCCEEDED if status == "succeeded" else FAILED, str(status))

    return run_node


def _real_build_service(node, svc_dir, budget, ledger) -> str:
    """Build one service through the existing scope->plan->build->verify pipeline, sharing the
    system budget. Returns the run's terminal status string."""
    from .cli import build_pipeline_phases
    from .config import Config
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
    phases, gates = build_pipeline_phases(
        str(Path(svc_dir) / "prd.md"), build=True, out_dir=out_dir,
        run_id=f"svc-{node.name}", executor=executor, verifier=verifier)
    orch = Orchestrator(phases=phases, gates=gates, budget=budget, ledger=ledger,
                        sandbox=NullSandbox())
    return orch.run()


def make_bring_up(run_dir, *, ensure_network=None, start_service=None, start_target=None):
    """Return bring_up(design) -> (base_urls, teardown). Starts each built service on a fresh
    per-run docker network (reusing deploy helpers) and collects {service_id -> base_url};
    teardown() removes every container this call started plus the network. Deploy helpers are
    injectable for tests."""
    from . import deploy
    en = ensure_network or deploy.ensure_network
    ss = start_service or deploy.start_service
    st = start_target or deploy.start_target
    run_dir = Path(run_dir)

    def bring_up(design):
        net = f"devagent-sys-{run_dir.name}"              # per-run: never shared across builds
        en(net)
        base_urls = {}
        started = []  # container names this call actually started, for teardown

        def teardown():
            # Containers run --restart unless-stopped, so `docker network rm` alone leaves
            # them attached and fails silently. Remove containers first, then the network.
            # check=False and try/except so this never raises (tests call it without docker).
            for container in started:
                try:
                    subprocess.run(["docker", "rm", "-f", container],
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
                out_dir = str(run_dir / "services" / node.name / "out")
                if node.kind in ("datastore", "service"):
                    container = ss(node, network=net)      # datastore: no base_url exposed
                    if container:
                        started.append(container)
                else:
                    url = st(out_dir, node, network=net)
                    if url:
                        base_urls[sid] = url
                        # start_target returns a URL, not the container name — but deploy.py
                        # names every container it starts devagent-preview-<target.name>.
                        started.append(f"devagent-preview-{node.name}")
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
                 architect=None, integration_runner=None) -> SystemReport:
    """Deterministic system-build orchestration. `run_node`, `bring_up`, `architect`, and
    `integration_runner` are injected (defaults do the real thing) so this is unit-testable
    without Docker/tokens."""
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
