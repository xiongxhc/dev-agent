"""Phase protocol + the shared per-run context handed to every phase.

A phase is input -> bounded work in the sandbox -> PhaseResult. Phases never touch
the host filesystem directly; all work goes through ctx.sandbox.

Inter-phase data flow (M2): a phase returns a typed `output_artifact`; the orchestrator
stuffs it into ctx.artifacts[phase.name] so downstream phases (spec reads intake's Brief,
build reads the frozen Spec+Plan) can consume it. The orchestrator is the only writer."""

from dataclasses import dataclass, field
from typing import Any, Protocol


class SandboxLike(Protocol):
    """An entered sandbox: runs a command, returns (exit, stdout, stderr)."""

    def run(self, cmd) -> tuple[int, str, str]: ...


class SandboxCtx(Protocol):
    """A sandbox context manager yielding a SandboxLike."""

    def __enter__(self) -> SandboxLike: ...

    def __exit__(self, *exc) -> object: ...


@dataclass
class PhaseResult:
    name: str
    exit_code: int
    output: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    output_artifact: Any = None  # typed payload threaded to downstream phases (M2)


@dataclass
class PhaseContext:
    sandbox: SandboxLike
    budget: Any  # Budget — kept Any to avoid an import cycle; tightened at the M2 boundary
    ledger: Any  # Ledger
    artifacts: dict[str, Any] = field(default_factory=dict)  # phase name -> output_artifact


class Phase(Protocol):
    name: str

    def run(self, ctx: PhaseContext) -> PhaseResult: ...
