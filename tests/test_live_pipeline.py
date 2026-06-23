"""Live brain-pipeline e2e — spends real tokens. Runs ONLY when DEVAGENT_RUN_LIVE=1
(so a normal `pytest` run, even with a key in the env, never bills)."""

import json
import os

import pytest

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.getenv("DEVAGENT_RUN_LIVE") != "1",
    reason="set DEVAGENT_RUN_LIVE=1 to run the live pipeline (spends tokens)",
)
def test_brain_pipeline_live_produces_valid_plan(tmp_path, monkeypatch):
    from devagent import cli

    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))
    rc = cli.main(["run", "examples/hello.md"])
    assert rc == 0

    rd = next(iter(tmp_path.glob("run-*")))
    plan = json.loads((rd / "plan.json").read_text())
    spec = json.loads((rd / "spec.json").read_text())
    assert len(plan["tasks"]) >= 1
    assert len(spec["acceptance_checks"]) >= 1
