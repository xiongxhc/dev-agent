"""`devagent run <input>` — wire up config, sandbox, the no-op phase + its gate,
budget, and ledger, then run the orchestrator and report the run id + status.

A `build_sandbox` hook is injectable so tests can run the full wiring against a fake
sandbox without Docker."""

import argparse
import sys
import time
import uuid
from pathlib import Path

from .budget import Budget
from .config import Config
from .gates import ContainerExitZero
from .ledger import Ledger
from .orchestrator import SUCCEEDED, Orchestrator
from .phases.noop import NoopPhase
from .sandbox import Sandbox


def _new_run_id() -> str:
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _default_build_sandbox(run_dir: Path, cfg: Config, ledger: Ledger) -> Sandbox:
    return Sandbox(run_dir=run_dir, image=cfg.image, ledger=ledger)


def main(argv: list[str] | None = None, *, build_sandbox=_default_build_sandbox) -> int:
    parser = argparse.ArgumentParser(prog="devagent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run the pipeline on an input (PRD file)")
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

    sandbox = build_sandbox(run_dir, cfg, ledger)
    budget = Budget(cfg.max_tokens, cfg.max_seconds, cfg.max_retries)
    orch = Orchestrator(
        phases=[NoopPhase()],
        gates={"noop": ContainerExitZero()},
        budget=budget,
        ledger=ledger,
        sandbox=sandbox,
    )
    status = orch.run()
    print(f"{run_id} {status}")
    return 0 if status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
