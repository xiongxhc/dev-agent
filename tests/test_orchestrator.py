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


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class RecordingPhase:
    """Runs nothing but records the call order; returns the given exit code."""

    def __init__(self, name, exit_code=0, sink=None):
        self.name = name
        self.exit_code = exit_code
        self.sink = sink if sink is not None else []

    def run(self, ctx):
        self.sink.append(self.name)
        return PhaseResult(self.name, self.exit_code, "")


class TokenBurnPhase:
    name = "burn"

    def run(self, ctx):
        ctx.budget.add_tokens(10_000)  # blows the ceiling -> BudgetExceeded
        return PhaseResult("burn", 0, "")


def make(phases, gates, sandbox, budget=None, tmp_path=None):
    budget = budget or Budget(max_tokens=1_000_000, max_seconds=1e9, max_retries=9)
    ledger = Ledger(tmp_path / "run")
    return Orchestrator(phases, gates, budget, ledger, sandbox), ledger


def test_runs_noop_and_succeeds(tmp_path):
    orch, ledger = make([NoopPhase()], {"noop": ContainerExitZero()},
                        FakeSandbox(), tmp_path=tmp_path)
    assert orch.run() == SUCCEEDED
    events = ledger.events()
    phase_ev = next(e for e in events if e["event"] == "phase" and e["phase"] == "noop")
    assert phase_ev["exit"] == 0 and phase_ev["output"] == NoopPhase.MARKER
    assert "meta" in phase_ev  # meta now persisted to the ledger (M2 carries tokens here)
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


def test_aborts_on_wallclock_before_any_phase_runs(tmp_path):
    # The pre-phase budget.tick() guard must abort a run that has blown wall-clock,
    # before the phase executes.
    clock = FakeClock(0.0)
    budget = Budget(max_tokens=10**9, max_seconds=10.0, max_retries=9, clock=clock)
    ran = []
    orch, ledger = make([RecordingPhase("spy", sink=ran)], {}, FakeSandbox(),
                        budget=budget, tmp_path=tmp_path)
    clock.now = 20.0  # past max_seconds
    assert orch.run() == ABORTED_BUDGET
    assert ran == []  # phase never executed


def test_phase_raising_non_budget_exception_records_terminal_status(tmp_path):
    class BoomPhase:
        name = "boom"

        def run(self, ctx):
            raise RuntimeError("kaboom")

    orch, ledger = make([BoomPhase()], {}, FakeSandbox(), tmp_path=tmp_path)
    assert orch.run() == FAILED
    end = ledger.events()[-1]
    assert end["event"] == "run_end" and end["status"] == FAILED
    assert "kaboom" in end["detail"]


def test_multiple_phases_run_in_order_gateless_phase_proceeds(tmp_path):
    order = []
    phases = [RecordingPhase("a", sink=order), RecordingPhase("b", sink=order)]
    # only "a" has a gate; "b" is gateless and must still run
    orch, ledger = make(phases, {"a": ContainerExitZero()}, FakeSandbox(), tmp_path=tmp_path)
    assert orch.run() == SUCCEEDED
    assert order == ["a", "b"]


def test_red_gate_short_circuits_remaining_phases(tmp_path):
    order = []
    phases = [RecordingPhase("a", exit_code=1, sink=order), RecordingPhase("b", sink=order)]
    orch, ledger = make(phases, {"a": ContainerExitZero()}, FakeSandbox(), tmp_path=tmp_path)
    assert orch.run() == FAILED
    assert order == ["a"]  # "b" never ran
