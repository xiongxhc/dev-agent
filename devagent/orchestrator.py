"""The deterministic phase state machine. Owns sequencing, gating, budget checks,
and stop conditions — the control flow stays in code, not in the model.

Every exit path writes a terminal run_end event so the ledger is always a complete
audit trail / resume source. The repair loop (M2) lives *inside* the build phase /
Executor (using the shared Budget.spend_retry/max_retries), NOT here — the orchestrator
loop stays dumb: one pass per phase, stop on red gate or budget breach.
"""

from dataclasses import dataclass

from .budget import Budget, BudgetExceeded
from .gates import Gate
from .ledger import Ledger
from .phases.base import Phase, PhaseContext, SandboxCtx

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
    sandbox: SandboxCtx

    def run(self) -> str:
        self.artifacts: dict = {}  # exposed to the caller (cli persists Brief/Spec/Plan)
        self.ledger.append({"event": "run_start", "phases": [p.name for p in self.phases]})
        try:
            with self.sandbox as sb:
                ctx = PhaseContext(sandbox=sb, budget=self.budget, ledger=self.ledger)
                self.artifacts = ctx.artifacts  # same ref — accumulates as phases run
                for phase in self.phases:
                    self.budget.tick()  # pre-phase wall-clock/retry/token guard
                    result = phase.run(ctx)
                    if result.output_artifact is not None:
                        ctx.artifacts[phase.name] = result.output_artifact
                    self.ledger.append({
                        "event": "phase", "phase": phase.name,
                        "exit": result.exit_code, "output": result.output,
                        "meta": result.meta,
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
        except Exception as e:  # noqa: BLE001 — every path must write a terminal run_end
            return self._finish(FAILED, repr(e))

    def _finish(self, status: str, detail: str = "") -> str:
        self.ledger.append({"event": "run_end", "status": status, "detail": detail})
        return status
