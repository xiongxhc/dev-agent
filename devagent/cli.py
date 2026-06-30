"""`devagent run <prd>` — the brain pipeline: scope -> plan [-> build -> deploy].

Each phase is a bounded LLM call gated deterministically. Brain phases run on the host
(NullSandbox); the build phase uses a real Docker sandbox (SdkExecutor or ManagedExecutor).
Produced artifacts (scope.json / plan.json) are written to runs/<id>/ for inspection."""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from . import egress
from .budget import Budget
from .config import Config
from .deploy import DeployGate
from .executor_sdk import SdkExecutor
from .ledger import Ledger
from .managed_executor import ManagedExecutor
from .orchestrator import SUCCEEDED, Orchestrator
from .phase_gates import PlanGate, ScopeGate, VerifyGate
from .phases.build import BuildPhase
from .phases.deploy import DeployPhase
from .phases.plan import PlanPhase
from .phases.scope import ScopePhase
from .report import write_report
from .sandbox import NullSandbox
from .verifier import BuildVerifier


def _new_run_id() -> str:
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devagent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run the pipeline on a PRD file")
    run_p.add_argument("input", help="path to a PRD/requirement file")
    run_p.add_argument("--build", action="store_true",
                       help="run the contained build phase (SdkExecutor; needs Docker + spends tokens)")
    run_p.add_argument("--answers", help="answers file for a prior clarification round")
    args = parser.parse_args(argv)

    if not Path(args.input).is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    cfg = Config.load()
    run_id = _new_run_id()
    run_dir = cfg.runs_dir / run_id
    ledger = Ledger(run_dir)
    ledger.append({"event": "input", "path": args.input})

    phases = [ScopePhase(args.input, answers_path=args.answers), PlanPhase()]
    gates = {"scope": ScopeGate(), "plan": PlanGate()}
    if args.build:
        # SdkExecutor self-contains its own disposable Docker container, so the
        # orchestrator's NullSandbox still suffices for the host-side brain phases.
        out_dir = run_dir / "out"
        # Egress allowlist for OUR containers (verify always; the sdk build too). The managed
        # arm builds on Anthropic's cloud sandbox, so it doesn't use our network — but verify
        # still re-checks the pulled output in our egress-contained container.
        network = proxy = None
        if cfg.egress:
            network, proxy = egress.ensure()
            ledger.append({"event": "egress", "network": network, "proxy": proxy})
        # The A/B seam: pick the build arm. Everything downstream (verify/acceptance/repair) is shared.
        ledger.append({"event": "executor", "kind": cfg.executor})
        if cfg.executor == "managed":
            executor = ManagedExecutor()
        else:
            executor = SdkExecutor(network=network, proxy_url=proxy, model=cfg.build_model)
        # BuildPhase owns the repair loop: build -> rebuild-from-source verify -> repair
        # (cap 2), then emits the VerifyReport. VerifyGate is the trusted final check.
        phases.append(BuildPhase(
            executor=executor, workdir=str(out_dir), run_id=run_id,
            verifier=BuildVerifier(network=network, proxy_url=proxy), max_repairs=2))
        gates["build"] = VerifyGate()
        # Deploy the built app to a local preview server -> URL (gated on it actually answering).
        phases.append(DeployPhase(workdir=str(out_dir)))
        gates["deploy"] = DeployGate()

    orch = Orchestrator(
        phases=phases,
        gates=gates,
        budget=Budget(cfg.max_tokens, cfg.max_seconds, cfg.max_retries,
                      max_cost_usd=cfg.max_cost_usd),
        ledger=ledger,
        sandbox=NullSandbox(),  # brain phases run on host; SdkExecutor contains its own
    )
    status = orch.run()

    # Persist the validated artifacts produced so far for inspection.
    for name, artifact in orch.artifacts.items():
        if hasattr(artifact, "model_dump_json"):
            (run_dir / f"{name}.json").write_text(artifact.model_dump_json(indent=2))

    # Always write a run report (even on failure — it reads the full ledger trail).
    deploy_art = orch.artifacts.get("deploy")
    preview_url = deploy_art.url if deploy_art is not None else None
    acc_path = run_dir / "out" / ".devagent" / "acceptance.json"
    acceptance = json.loads(acc_path.read_text()).get("checks") if acc_path.is_file() else None
    report_path = write_report(run_dir, ledger.events(), run_id,
                               preview_url=preview_url, acceptance=acceptance)

    if status != SUCCEEDED:
        scope = orch.artifacts.get("scope")
        clar = getattr(scope, "clarifications", None) if scope is not None else None
        if clar:
            print("  -> needs clarification before building:", file=sys.stderr)
            for q in clar:
                print(f"     - {q}", file=sys.stderr)
            print("  -> answer these in a file and re-run with --answers <file>", file=sys.stderr)

    print(f"{run_id} {status}")
    plan = orch.artifacts.get("plan")
    if status == SUCCEEDED and plan is not None:
        print(f"  -> {len(plan.tasks)} tasks; artifacts in {run_dir}")
    if status == SUCCEEDED and args.build:
        print(f"  -> built + verified app in {run_dir / 'out'}")
    if preview_url:
        print(f"  -> preview: {preview_url}")
    print(f"  -> report: {report_path}")
    return 0 if status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
