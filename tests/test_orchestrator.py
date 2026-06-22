from devagent.budget import Budget
from devagent.gates import ContainerExitZero
from devagent.ledger import Ledger
from devagent.orchestrator import ABORTED_BUDGET, FAILED, SUCCEEDED, Orchestrator
from devagent.phases.base import PhaseResult
from devagent.phases.noop import NoopPhase


class FakeSandbox:
    """Context manager standing in for the real Docker sandbox."""

    def __init__(self, result=(0, NoopPhase.MARKER + "\n", "")):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cmd):
        return self.result


class TokenBurnPhase:
    name = "burn"

    def run(self, ctx):
        ctx.budget.add_tokens(10_000)  # blows the ceiling -> BudgetExceeded
        return PhaseResult("burn", 0, "")


def make(phases, gates, sandbox, budget=None, ledger_dir=None, tmp_path=None):
    budget = budget or Budget(max_tokens=1_000_000, max_seconds=1e9, max_retries=9)
    ledger = Ledger(ledger_dir or (tmp_path / "run"))
    return Orchestrator(phases, gates, budget, ledger, sandbox), ledger


def test_runs_noop_and_succeeds(tmp_path):
    orch, ledger = make([NoopPhase()], {"noop": ContainerExitZero()},
                         FakeSandbox(), tmp_path=tmp_path)
    assert orch.run() == SUCCEEDED
    events = ledger.events()
    assert {"event": "phase", "phase": "noop", "exit": 0, "output": NoopPhase.MARKER} in events
    assert events[-1] == {"event": "run_end", "status": SUCCEEDED, "detail": ""}


def test_red_gate_stops_and_marks_failed(tmp_path):
    orch, ledger = make([NoopPhase()], {"noop": ContainerExitZero()},
                        FakeSandbox(result=(1, "boom\n", "")), tmp_path=tmp_path)
    assert orch.run() == FAILED
    end = ledger.events()[-1]
    assert end["status"] == FAILED
    gate_events = [e for e in ledger.events() if e["event"] == "gate"]
    assert gate_events and gate_events[0]["ok"] is False


def test_budget_breach_aborts_cleanly(tmp_path):
    budget = Budget(max_tokens=100, max_seconds=1e9, max_retries=9)
    orch, ledger = make([TokenBurnPhase()], {}, FakeSandbox(),
                        budget=budget, tmp_path=tmp_path)
    assert orch.run() == ABORTED_BUDGET
    assert ledger.events()[-1]["status"] == ABORTED_BUDGET
