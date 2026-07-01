# devagent/eval/scoring.py
"""Pure scoring: turn a (BuildResult, VerifyReport, JudgeVerdict?) into a RunScore, and fold a
fixture's runs into per-arm summaries. No I/O, no model — deterministic and unit-tested."""

from .schema import ArmSummary, JudgeVerdict, RunScore


def _all_in(cost_token_usd: float | None, wall_clock_s: float, session_hr_usd: float) -> float | None:
    """Token cost + compute-time cost. Returns None only when there is no signal at all
    (no token cost AND no wall-clock) — a 0-cost arm still yields its session-hr charge."""
    session = (wall_clock_s / 3600.0) * session_hr_usd if wall_clock_s else 0.0
    if cost_token_usd is None and not session:
        return None
    return round((cost_token_usd or 0.0) + session, 6)


def score_run(arm: str, run_index: int, build_result, verify_report,
              verdict: JudgeVerdict | None, session_hr_usd: float) -> RunScore:
    checks = verify_report.checks if verify_report is not None else []
    passed = sum(1 for c in checks if getattr(c, "ok", False))
    cost_token = getattr(build_result, "cost_usd", None)
    wall = getattr(build_result, "wall_clock_s", 0.0) or 0.0
    return RunScore(
        arm=arm,
        run_index=run_index,
        acceptance_pass=bool(verify_report is not None and verify_report.ok),
        build_ok=bool(verify_report is not None and verify_report.build_ok),
        dist_present=bool(verify_report is not None and verify_report.dist_present),
        checks_total=len(checks),
        checks_passed=passed,
        tokens_in=getattr(build_result, "tokens_in", 0) or 0,
        tokens_out=getattr(build_result, "tokens_out", 0) or 0,
        cache_read_tokens=getattr(build_result, "cache_read_tokens", 0) or 0,
        wall_clock_s=wall,
        cost_token_usd=cost_token,
        cost_all_in_usd=_all_in(cost_token, wall, session_hr_usd),
        judge_overall=verdict.overall if verdict else None,
        judge_scores={s.criterion: s.score for s in verdict.scores} if verdict else {},
        error=getattr(build_result, "error", None),
    )


def _mean(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def summarize_arm(arm: str, runs: list[RunScore], *, unavailable: bool = False) -> ArmSummary:
    """Fold one arm's runs into a summary. An unavailable arm (or one with no runs) reports
    an empty summary flagged accordingly — it is never counted as a 0% pass."""
    arm_runs = [r for r in runs if r.arm == arm]
    if unavailable or not arm_runs:
        return ArmSummary(arm=arm, runs=0, acceptance_pass_rate=0.0, mean_judge_overall=None,
                          mean_cost_token_usd=None, mean_cost_all_in_usd=None,
                          mean_wall_clock_s=None, unavailable=unavailable)
    n = len(arm_runs)
    return ArmSummary(
        arm=arm,
        runs=n,
        acceptance_pass_rate=round(sum(1 for r in arm_runs if r.acceptance_pass) / n, 6),
        mean_judge_overall=_mean([r.judge_overall for r in arm_runs]),
        mean_cost_token_usd=_mean([r.cost_token_usd for r in arm_runs]),
        mean_cost_all_in_usd=_mean([r.cost_all_in_usd for r in arm_runs]),
        mean_wall_clock_s=_mean([r.wall_clock_s for r in arm_runs]),
    )
