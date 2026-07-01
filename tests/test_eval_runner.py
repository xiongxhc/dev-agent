"""M5 eval — the orchestrator: freeze-once, arm fairness, N runs, resumability, graceful skip,
and per-run failure isolation. All seams faked (no Docker, no tokens)."""

import json
import types

from devagent.eval.corpus import Corpus, Fixture
from devagent.eval.runner import EvalRunner
from devagent.eval.schema import CriterionScore, JudgeVerdict
from devagent.schema import ArtifactSpec, Plan, ProjectScope, Task


def _scope():
    return ProjectScope(title="T", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web")])


def _plan():
    return Plan(tasks=[Task(id="a", description="x", owned_files=["web/a"])])


def _build(err=None):
    return types.SimpleNamespace(cost_usd=0.1, wall_clock_s=10.0, tokens_in=100, tokens_out=50,
                                 cache_read_tokens=0, repo_path="/x", error=err)


def _verify(ok=True):
    return types.SimpleNamespace(ok=ok, build_ok=True, dist_present=True,
                                 checks=[types.SimpleNamespace(ok=ok)])


def _verdict():
    return JudgeVerdict(scores=[CriterionScore(criterion="code_quality", score=4)], overall=4)


def _corpus(tmp_path, arms=("sdk", "managed"), n=2):
    return Corpus(fixtures=[Fixture("hello", tmp_path / "hello.md")], arms=list(arms), n=n)


def test_freezes_brain_once_and_both_arms_build_identical_bytes(tmp_path):
    brain_calls, build_calls = [], []

    def brain(fixture, work):
        brain_calls.append(fixture.name)
        return _scope(), _plan(), None

    def build(arm, s, p, run_dir):
        build_calls.append((arm, json.dumps(s.model_dump(), sort_keys=True),
                            json.dumps(p.model_dump(), sort_keys=True)))
        return _build(), _verify()

    res = EvalRunner(tmp_path / "eval", _corpus(tmp_path),
                     brain_fn=brain, build_fn=build, judge_fn=lambda s, r: _verdict()).run()

    assert brain_calls == ["hello"]                      # scope+plan frozen ONCE per fixture
    assert len(build_calls) == 4                         # 2 arms × N=2
    assert len({c[1] for c in build_calls}) == 1         # fairness: identical scope bytes to all
    assert len({c[2] for c in build_calls}) == 1         # identical plan bytes to all
    fx = res.fixtures[0]
    assert {a.arm: a.runs for a in fx.arms} == {"sdk": 2, "managed": 2}
    assert all(a.acceptance_pass_rate == 1.0 for a in fx.arms)


def test_resumes_without_rerunning_completed_work(tmp_path):
    calls = {"brain": 0, "build": 0}

    def brain(fixture, work):
        calls["brain"] += 1
        return _scope(), _plan(), None

    def build(arm, s, p, run_dir):
        calls["build"] += 1
        return _build(), _verify()

    def make():
        return EvalRunner(tmp_path / "eval", _corpus(tmp_path),
                          brain_fn=brain, build_fn=build, judge_fn=lambda s, r: None)

    make().run()
    assert calls == {"brain": 1, "build": 4}
    make().run()                                         # resume on the same eval dir
    assert calls == {"brain": 1, "build": 4}             # nothing re-run — all cached


def test_unavailable_arm_skipped_gracefully(tmp_path):
    build_arms = []

    def build(arm, s, p, run_dir):
        build_arms.append(arm)
        return _build(), _verify()

    res = EvalRunner(tmp_path / "eval", _corpus(tmp_path),
                     brain_fn=lambda f, w: (_scope(), _plan(), None), build_fn=build,
                     judge_fn=lambda s, r: None, arm_available=lambda a: a != "managed").run()

    assert set(build_arms) == {"sdk"}                    # managed never built
    arms = {a.arm: a for a in res.fixtures[0].arms}
    assert arms["managed"].unavailable is True and arms["managed"].runs == 0
    assert arms["sdk"].runs == 2


def test_brain_failure_records_scope_error_and_builds_nothing(tmp_path):
    built = []
    res = EvalRunner(tmp_path / "eval", _corpus(tmp_path),
                     brain_fn=lambda f, w: (None, None, "needs clarification"),
                     build_fn=lambda *a: built.append(1) or (_build(), _verify()),
                     judge_fn=lambda s, r: None).run()
    fx = res.fixtures[0]
    assert fx.scope_error == "needs clarification"
    assert fx.runs == [] and built == []                 # no build attempted


def test_build_exception_becomes_a_failed_run_not_a_corpus_abort(tmp_path):
    def build(arm, s, p, run_dir):
        raise RuntimeError("docker died")

    res = EvalRunner(tmp_path / "eval", _corpus(tmp_path, arms=("sdk",), n=1),
                     brain_fn=lambda f, w: (_scope(), _plan(), None),
                     build_fn=build, judge_fn=lambda s, r: None).run()
    run = res.fixtures[0].runs[0]
    assert run.acceptance_pass is False and "docker died" in run.error
    assert res.fixtures[0].arms[0].acceptance_pass_rate == 0.0
