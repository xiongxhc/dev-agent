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
    scope = json.loads((rd / "scope.json").read_text())
    assert len(plan["tasks"]) >= 1
    assert len(scope["targets"]) >= 1


@pytest.mark.skipif(os.getenv("DEVAGENT_RUN_LIVE") != "1", reason="live")
def test_live_fullstack_build(tmp_path):
    from devagent.cli import main
    rc = main(["run", "--build", "examples/fullstack.md"])
    assert rc == 0   # scope(web+api) -> build -> per-target rebuild + boot + api_json + route_status -> deploy


@pytest.mark.skipif(os.getenv("DEVAGENT_RUN_LIVE") != "1", reason="live")
def test_live_fullstack_persistent_build(tmp_path):
    from devagent.cli import main
    rc = main(["run", "--build", "examples/fullstack-persistent.md"])
    assert rc == 0   # scope picks a persistence strategy -> build -> verify with a real
                     # datastore (if chosen) + persistence_survives_restart -> deploy


@pytest.mark.skipif(os.getenv("DEVAGENT_RUN_LIVE") != "1", reason="live")
def test_live_fullstack_shared_state_build(tmp_path):
    # The PRD's shape (multiple stateless API replicas sharing state) nudges the agent toward a
    # MANAGED datastore — exercising the sibling-container verify/deploy path (postgres/mongo)
    # that the SQLite fixture does not. The agent still decides; this asserts the whole pipeline
    # succeeds end-to-end whichever store it picks.
    from devagent.cli import main
    rc = main(["run", "--build", "examples/fullstack-persistent-shared.md"])
    assert rc == 0   # scope -> build -> verify (sibling datastore + persistence_survives_restart) -> deploy
