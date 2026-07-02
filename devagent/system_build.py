"""M20 — system-build wiring. Runs Architect -> per-service builds (M15 TreeOrchestrator) ->
multi-container bring-up -> cross-service E2E (M17). The two side-effecting pieces (build one
service; bring the services up) are injected callables so the orchestration is unit-testable
without Docker/tokens; the defaults do the real thing. Nothing in M14-M17 is modified."""

from pathlib import Path

from .tree import NodeResult, SUCCEEDED, FAILED


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
