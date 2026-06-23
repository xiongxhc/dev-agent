"""Plan brain phase: decompose the frozen Spec into an ordered, parallelizable Plan.

Host-side — calls the LLM, emits an artifact, never touches the sandbox. The prompt
forces DISJOINT owned_files across tasks (the Plan schema also enforces this) so build
subagents never collide on a file."""

from ..llm import generate_structured
from ..schema import Plan
from .base import PhaseContext, PhaseResult

_PROMPT = """\
Decompose this Spec into an ordered Plan of build tasks.

Rules:
- Order tasks so earlier tasks unblock later ones (scaffolding/shared bits first).
- owned_files MUST be DISJOINT across tasks: no file may appear in more than one task's
  owned_files. Each task fully owns the files it lists. Split the work along file
  boundaries so parallel build subagents never touch the same file.
- Give each task a short stable id and a one-line description.

Spec title: {title}
Stack: {stack}
Pages: {pages}
Components: {components}
"""


class PlanPhase:
    name = "plan"

    def run(self, ctx: PhaseContext) -> PhaseResult:
        try:
            spec = ctx.artifacts["spec"]
            prompt = _PROMPT.format(
                title=spec.title,
                stack=spec.stack,
                pages=", ".join(spec.pages),
                components=", ".join(spec.components),
            )
            plan, usage = generate_structured(prompt, Plan)
            return PhaseResult(
                name=self.name,
                exit_code=0,
                output=f"{len(plan.tasks)} tasks",
                meta=usage,
                output_artifact=plan,
            )
        except Exception as e:
            return PhaseResult(name=self.name, exit_code=1, output=str(e))
