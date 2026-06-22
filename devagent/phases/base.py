"""Phase protocol + the shared per-run context handed to every phase.

A phase is input -> bounded work in the sandbox -> PhaseResult. Phases never touch
the host filesystem directly; all work goes through ctx.sandbox."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PhaseResult:
    name: str
    exit_code: int
    output: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseContext:
    sandbox: Any  # anything with .run(cmd) -> (exit, stdout, stderr)
    budget: Any
    ledger: Any


class Phase(Protocol):
    name: str

    def run(self, ctx: PhaseContext) -> PhaseResult: ...
