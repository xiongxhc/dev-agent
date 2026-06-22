"""Gates — the deterministic spine. A gate is plain code that says pass/fail on a
PhaseResult. Never the model's self-assessment."""

from dataclasses import dataclass
from typing import Protocol

from .phases.base import PhaseResult


@dataclass
class GateResult:
    ok: bool
    reason: str = ""


class Gate(Protocol):
    name: str

    def check(self, result: PhaseResult) -> GateResult: ...


@dataclass
class ContainerExitZero:
    """Passes iff the phase's sandbox command exited 0."""

    name: str = "container_exit_zero"

    def check(self, result: PhaseResult) -> GateResult:
        if result.exit_code == 0:
            return GateResult(True)
        return GateResult(False, f"{result.name} exited {result.exit_code}: {result.output!r}")
