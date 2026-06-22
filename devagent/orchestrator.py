"""The deterministic phase state machine. Owns sequencing, gating, budget checks,
and stop conditions — the control flow stays in code, not in the model.

M1 runs a single phase. The loop shape is the same one M2+ grows into:
  for phase: budget.check -> phase.run -> gate.check -> ledger; stop on red/breach.
"""

from dataclasses import dataclass, field

from .budget import Budget, BudgetExceeded
from .gates import Gate
from .ledger import Ledger
from .phases.base import Phase, PhaseContext

# Terminal run statuses recorded to the ledger.
SUCCEEDED = "succeeded"
FAILED = "failed"
ABORTED_BUDGET = "aborted_budget"


@dataclass
class Orchestrator:
    phases: list[Phase]
    gates: dict[str, Gate]
    budget: Budget
    ledger: Ledger
    sandbox: object  # context manager yielding something with .run()

    def run(self) -> str:
        self.ledger.append({"event": "run_start", "phases": [p.name for p in self.phases]})
        try:
            with self.sandbox as sb:
                ctx = PhaseContext(sandbox=sb, budget=self.budget, ledger=self.ledger)
                for phase in self.phases:
                    self.budget.tick()
                    result = phase.run(ctx)
                    self.ledger.append({
                        "event": "phase", "phase": phase.name,
                        "exit": result.exit_code, "output": result.output,
                    })
                    gate = self.gates.get(phase.name)
                    if gate is not None:
                        gr = gate.check(result)
                        self.ledger.append({
                            "event": "gate", "phase": phase.name,
                            "gate": gate.name, "ok": gr.ok, "reason": gr.reason,
                        })
                        if not gr.ok:
                            return self._finish(FAILED, f"gate {gate.name} failed: {gr.reason}")
            return self._finish(SUCCEEDED)
        except BudgetExceeded as e:
            return self._finish(ABORTED_BUDGET, str(e))

    def _finish(self, status: str, detail: str = "") -> str:
        self.ledger.append({"event": "run_end", "status": status, "detail": detail})
        return status
