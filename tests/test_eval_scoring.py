"""M5 eval — pure scoring: RunScore mapping + per-arm aggregation. No I/O, no model."""

import types

from devagent.eval.schema import CriterionScore, JudgeVerdict
from devagent.eval.scoring import score_run, summarize_arm


def _build(cost=0.10, wall=3600.0, tin=1000, tout=500, repo="/x", err=None):
    return types.SimpleNamespace(cost_usd=cost, wall_clock_s=wall, tokens_in=tin, tokens_out=tout,
                                 cache_read_tokens=200, repo_path=repo, error=err)


def _verify(ok=True, build_ok=True, dist=True, checks=(True, False)):
    return types.SimpleNamespace(ok=ok, build_ok=build_ok, dist_present=dist,
                                 checks=[types.SimpleNamespace(ok=c) for c in checks])


def _verdict(overall=4):
    return JudgeVerdict(scores=[CriterionScore(criterion="code_quality", score=4)], overall=overall)


def test_score_run_maps_all_axes():
    s = score_run("sdk", 0, _build(), _verify(), _verdict(), session_hr_usd=2.0)
    assert s.acceptance_pass is True
    assert s.checks_total == 2 and s.checks_passed == 1
    assert s.cost_token_usd == 0.10
    assert s.cost_all_in_usd == 2.10          # 0.10 token + 1 wall-hour × $2.0
    assert s.judge_overall == 4 and s.judge_scores == {"code_quality": 4}


def test_all_in_cost_includes_session_hr_even_when_token_cost_absent():
    s = score_run("managed", 0, _build(cost=None, wall=1800.0), _verify(), None, session_hr_usd=2.0)
    assert s.cost_token_usd is None
    assert s.cost_all_in_usd == 1.0           # 0.5h × $2.0, no token cost


def test_failed_build_scores_as_no_acceptance():
    s = score_run("sdk", 1, _build(err="tsc failed"), _verify(ok=False, build_ok=False, dist=False,
                                                              checks=()), None, session_hr_usd=2.0)
    assert s.acceptance_pass is False and s.build_ok is False
    assert s.checks_total == 0 and s.judge_overall is None
    assert s.error == "tsc failed"


def test_summarize_arm_aggregates_pass_rate_and_means():
    runs = [
        score_run("sdk", 0, _build(cost=0.10, wall=3600.0), _verify(ok=True), _verdict(4), 2.0),
        score_run("sdk", 1, _build(cost=0.30, wall=3600.0), _verify(ok=False), _verdict(2), 2.0),
    ]
    summ = summarize_arm("sdk", runs)
    assert summ.runs == 2
    assert summ.acceptance_pass_rate == 0.5
    assert summ.mean_judge_overall == 3.0
    assert summ.mean_cost_token_usd == 0.2    # (0.10 + 0.30)/2
    assert summ.mean_cost_all_in_usd == 2.2   # each 0.x + $2.0 → mean 2.2


def test_summarize_unavailable_arm_is_not_a_zero_pass():
    summ = summarize_arm("managed", [], unavailable=True)
    assert summ.unavailable is True and summ.runs == 0
    assert summ.mean_cost_token_usd is None   # nothing measured, not "$0"


def test_summarize_arm_ignores_other_arms_runs():
    runs = [score_run("sdk", 0, _build(), _verify(), None, 2.0)]
    summ = summarize_arm("managed", runs)     # no managed runs present
    assert summ.runs == 0
