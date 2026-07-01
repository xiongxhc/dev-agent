"""M5 eval — the blinded judge: repo digest + a scoring call that never sees the arm."""

from devagent.eval import judge as judge_mod
from devagent.eval.judge import digest_repo, judge_build, spec_summary
from devagent.eval.schema import CriterionScore, JudgeVerdict


def test_digest_skips_deps_and_build_output(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const x = 1")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("noise")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("built")
    d = digest_repo(tmp_path)
    assert "src/app.ts" in d and "export const x = 1" in d
    assert "node_modules" not in d and "bundle.js" not in d   # deps + build output excluded


def test_digest_missing_repo():
    assert digest_repo("/no/such/path") == "(no repo produced)"


def test_judge_build_is_blinded_and_returns_verdict(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("print('hi')")
    captured = {}

    def fake_gen(prompt, schema, client=None):
        captured["prompt"] = prompt
        return JudgeVerdict(scores=[CriterionScore(criterion="code_quality", score=4)], overall=4), {}

    monkeypatch.setattr(judge_mod, "generate_structured", fake_gen)
    verdict = judge_build(spec_summary({"title": "Todo", "targets": []}), tmp_path)
    assert verdict.overall == 4
    # blinded: the prompt carries the spec + built source but nothing about which arm produced it
    p = captured["prompt"].lower()
    assert "todo" in p and "app.py" in p
    assert "sdk" not in p and "managed" not in p and "arm" not in p
