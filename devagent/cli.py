"""`devagent run <prd>` — the M2 brain pipeline: intake -> spec -> plan, each a bounded
LLM call gated deterministically. Brain phases run on the host (NullSandbox); the build
phase (next increment) uses the real Docker sandbox. Produced artifacts (Brief/Spec/Plan)
are written to runs/<id>/ as JSON for inspection."""

import argparse
import sys
import time
import uuid
from pathlib import Path

from .budget import Budget
from .config import Config
from .ledger import Ledger
from .orchestrator import SUCCEEDED, Orchestrator
from .phase_gates import BriefGate, PlanGate, SpecGate
from .phases.intake import IntakePhase
from .phases.plan import PlanPhase
from .phases.spec import SpecPhase
from .sandbox import NullSandbox


def _new_run_id() -> str:
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devagent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run the pipeline on a PRD file")
    run_p.add_argument("input", help="path to a PRD/requirement file")
    args = parser.parse_args(argv)

    if not Path(args.input).is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    cfg = Config.load()
    run_id = _new_run_id()
    run_dir = cfg.runs_dir / run_id
    ledger = Ledger(run_dir)
    ledger.append({"event": "input", "path": args.input})

    orch = Orchestrator(
        phases=[IntakePhase(args.input), SpecPhase(), PlanPhase()],
        gates={"intake": BriefGate(), "spec": SpecGate(), "plan": PlanGate()},
        budget=Budget(cfg.max_tokens, cfg.max_seconds, cfg.max_retries),
        ledger=ledger,
        sandbox=NullSandbox(),  # brain phases run on host; build phase (later) uses Sandbox
    )
    status = orch.run()

    # Persist the validated artifacts produced so far for inspection.
    for name, artifact in orch.artifacts.items():
        if hasattr(artifact, "model_dump_json"):
            (run_dir / f"{name}.json").write_text(artifact.model_dump_json(indent=2))

    print(f"{run_id} {status}")
    plan = orch.artifacts.get("plan")
    if status == SUCCEEDED and plan is not None:
        print(f"  -> {len(plan.tasks)} tasks; artifacts in {run_dir}")
    return 0 if status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
