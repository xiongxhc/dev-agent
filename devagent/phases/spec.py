"""Spec brain phase: turn the intake Brief into a validated, machine-checkable Spec.

Host-side — calls the LLM, emits an artifact, never touches the sandbox. The prompt
pins the stack and forces every acceptance check onto a real route so the verify phase
can compute a bool from each without the model."""

from ..llm import generate_structured
from ..schema import Spec
from .base import PhaseContext, PhaseResult

_PROMPT = """\
You are writing the Spec for a {stack} web app, based on this Brief.

Rules:
- pages MUST be ROUTE PATHS, e.g. "/" or "/about" or "/items/new" — never prose
  descriptions. Each entry is a single path starting with "/".
- components is a list of React component names the app needs.
- acceptance_checks must each be machine-checkable. Every check's `route` MUST be one
  of the pages you listed above. Use kind="route_status" to assert a page loads, and
  kind="selector_present" (with a CSS `selector`) to assert a key element renders on a
  page. Include at least one check per page.

Brief title: {title}
Summary: {summary}
Requirements:
{requirements}
"""


class SpecPhase:
    name = "spec"

    def run(self, ctx: PhaseContext) -> PhaseResult:
        try:
            brief = ctx.artifacts["intake"]
            prompt = _PROMPT.format(
                stack=Spec.model_fields["stack"].default,
                title=brief.title,
                summary=brief.summary,
                requirements="\n".join(f"- {r}" for r in brief.requirements),
            )
            spec, usage = generate_structured(prompt, Spec)
            return PhaseResult(
                name=self.name,
                exit_code=0,
                output=spec.title,
                meta=usage,
                output_artifact=spec,
            )
        except Exception as e:
            return PhaseResult(name=self.name, exit_code=1, output=str(e))
