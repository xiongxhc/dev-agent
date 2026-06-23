"""Containment end-to-end: the no-op phase runs in a REAL --rm sandbox via the
orchestrator. Kept independent of the CLI (whose `run` is now the live brain pipeline)
so the M1 containment proof stays a pure, token-free Docker test."""

import pytest

from devagent.budget import Budget
from devagent.gates import ContainerExitZero
from devagent.ledger import Ledger
from devagent.orchestrator import SUCCEEDED, Orchestrator
from devagent.phases.noop import NoopPhase
from devagent.sandbox import Sandbox

pytestmark = pytest.mark.docker


def test_noop_runs_in_real_sandbox_end_to_end(tmp_path, sandbox_image):
    ledger = Ledger(tmp_path / "run")
    sandbox = Sandbox(tmp_path / "run", image=sandbox_image, ledger=ledger)
    orch = Orchestrator(
        phases=[NoopPhase()],
        gates={"noop": ContainerExitZero()},
        budget=Budget(10**9, 1e9, 9),
        ledger=ledger,
        sandbox=sandbox,
    )
    assert orch.run() == SUCCEEDED
    events = ledger.events()
    kinds = [e["event"] for e in events]
    assert "sandbox_start" in kinds
    assert events[-1] == {"event": "run_end", "status": SUCCEEDED, "detail": ""}
