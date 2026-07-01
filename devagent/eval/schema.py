# devagent/eval/schema.py
"""Eval result types. Internal results are dataclasses (like BuildResult/VerifyReport); the
judge's output is a pydantic model so the LLM returns it via forced tool-use, validated.

SESSION_HR_USD is the wall-clock rate used to normalize an arm's *all-in* cost (compute time on
top of model tokens). The managed arm already folds its session-hr into `cost_usd`; the SDK arm's
container wall-hours are charged at the same rate here so the two arms compare apples-to-apples."""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

# Same knob the managed arm uses for its session-hr charge (see managed_executor.SESSION_HR_USD).
# Override per-eval via DEVAGENT_SESSION_HR_USD. A rough blended compute rate, not a billing figure.
SESSION_HR_USD_DEFAULT = 2.0

# The blinded judge rubric — fixed criteria so scores are comparable across arms and fixtures.
RUBRIC = ("spec_completeness", "code_quality", "ux_craft")


class CriterionScore(BaseModel):
    criterion: str
    score: int = Field(..., ge=1, le=5)
    reason: str = ""


class JudgeVerdict(BaseModel):
    """A blinded per-criterion score of one built app (the judge never sees which arm built it)."""

    scores: list[CriterionScore] = Field(..., min_length=1)
    overall: int = Field(..., ge=1, le=5)


@dataclass
class RunScore:
    """One build run of one arm on one fixture — the atom the report aggregates."""

    arm: str
    run_index: int
    acceptance_pass: bool           # VerifyReport.ok — the authoritative quality signal
    build_ok: bool
    dist_present: bool
    checks_total: int
    checks_passed: int
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    wall_clock_s: float = 0.0
    cost_token_usd: float | None = None   # model-token cost (as the arm reported it)
    cost_all_in_usd: float | None = None  # token cost + wall-hours × SESSION_HR_USD
    judge_overall: int | None = None      # blinded judge 1–5 (None = not judged / build failed)
    judge_scores: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ArmSummary:
    arm: str
    runs: int
    acceptance_pass_rate: float           # fraction of runs that passed acceptance
    mean_judge_overall: float | None      # over judged runs
    mean_cost_token_usd: float | None
    mean_cost_all_in_usd: float | None
    mean_wall_clock_s: float | None
    unavailable: bool = False             # the arm couldn't run in this env (e.g. managed beta gated)


@dataclass
class FixtureResult:
    fixture: str                          # PRD path/name
    title: str                            # frozen scope title (or the fixture name)
    scope_error: str | None = None        # set if the brain (scope/plan) never produced buildable bytes
    runs: list[RunScore] = field(default_factory=list)
    arms: list[ArmSummary] = field(default_factory=list)


@dataclass
class EvalResult:
    eval_id: str
    fixtures: list[FixtureResult] = field(default_factory=list)
