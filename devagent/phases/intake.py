"""Intake brain phase: read a PRD file and normalize it into a validated Brief.

Host-side — calls the LLM, emits an artifact, never touches the sandbox. On any
LLM/validation failure it returns exit_code=1 rather than raising, so the orchestrator
sees a clean failed PhaseResult."""

from ..llm import generate_structured
from ..schema import Brief
from .base import PhaseContext, PhaseResult

_PROMPT = """\
You are normalizing a product requirements doc into a structured Brief.

Set source="prd". Produce a concise title and summary, and extract the concrete
requirements as a flat list of short imperative statements (one capability each).

PRD:
{prd}
"""


class IntakePhase:
    name = "intake"

    def __init__(self, input_path: str):
        self.input_path = input_path

    def run(self, ctx: PhaseContext) -> PhaseResult:
        try:
            with open(self.input_path) as f:
                prd = f.read()
            brief, usage = generate_structured(_PROMPT.format(prd=prd), Brief)
            return PhaseResult(
                name=self.name,
                exit_code=0,
                output=brief.title,
                meta=usage,
                output_artifact=brief,
            )
        except Exception as e:
            return PhaseResult(name=self.name, exit_code=1, output=str(e))
