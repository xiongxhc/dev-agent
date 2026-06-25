"""Plan brain phase: decompose the confirmed ProjectScope into an ordered, parallelizable
Plan spanning ALL targets. owned_files are DISJOINT across tasks (the Plan schema enforces
this too) so build subagents never collide — across targets, files live under each target's
dir (e.g. web/src/..., api/src/...)."""

import json

from ..llm import generate_structured
from ..schema import Plan
from .base import PhaseContext, PhaseResult

_PROMPT = """\
Decompose this multi-target project into an ordered Plan of build tasks.

Rules:
- Cover EVERY target. Put each target's files under its own directory: `<target.name>/...`.
- Order tasks so earlier tasks unblock later ones (scaffolding/shared bits first).
- owned_files MUST be DISJOINT across tasks: no file in more than one task. Split along
  file boundaries so parallel build subagents never touch the same file.
- Give each task a short stable id and a one-line description.

PROJECT: {title}
TARGETS (name | type | stack):
{targets}

TARGET DETAIL (JSON):
{detail}
"""


class PlanPhase:
    name = "plan"

    def __init__(self, client=None):
        self.client = client

    def run(self, ctx: PhaseContext) -> PhaseResult:
        try:
            scope = ctx.artifacts["scope"]
            targets = "\n".join(f"- {t.name} | {t.type} | {t.stack}" for t in scope.targets)
            detail = json.dumps({t.name: t.detail for t in scope.targets}, indent=2)
            prompt = _PROMPT.format(title=scope.title, targets=targets, detail=detail)
            plan, usage = generate_structured(prompt, Plan, client=self.client)
            return PhaseResult(name=self.name, exit_code=0,
                               output=f"{len(plan.tasks)} tasks", meta=usage,
                               output_artifact=plan)
        except Exception as e:
            return PhaseResult(name=self.name, exit_code=1, output=str(e))
