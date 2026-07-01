"""M5 eval — report render (JSON + HTML) from an EvalResult."""

import json

from devagent.eval.report import render_html, to_dict, write_report
from devagent.eval.schema import ArmSummary, EvalResult, FixtureResult


def _result():
    return EvalResult(eval_id="eval-1", fixtures=[
        FixtureResult(fixture="hello", title="Hello", arms=[
            ArmSummary("sdk", 2, 1.0, 4.5, 0.12, 0.20, 12.0),
            ArmSummary("managed", 0, 0.0, None, None, None, None, unavailable=True),
        ]),
        FixtureResult(fixture="broken", title="broken", scope_error="needs clarification"),
    ])


def test_to_dict_roundtrips_json():
    d = to_dict(_result())
    assert json.loads(json.dumps(d))["eval_id"] == "eval-1"
    assert d["fixtures"][0]["arms"][0]["arm"] == "sdk"


def test_html_shows_arms_costs_and_unavailable_and_scope_error():
    html = render_html(_result())
    assert "eval-1" in html and "Hello" in html
    assert "100%" in html                       # sdk acceptance pass-rate
    assert "$0.12" in html or "0.12" in html     # token cost
    assert "0.20" in html                        # all-in cost
    assert "unavailable" in html                 # managed arm flagged
    assert "needs clarification" in html         # scope-failed fixture surfaced


def test_write_report_emits_both_files(tmp_path):
    jp, hp = write_report(tmp_path, _result())
    assert jp.is_file() and hp.is_file()
    assert json.loads(jp.read_text())["eval_id"] == "eval-1"
    assert "<table" in hp.read_text()
