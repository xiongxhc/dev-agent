"""CLI wiring of the brain pipeline — no live tokens (the phases' LLM call is patched)."""

from devagent import cli
from devagent.phases.base import PhaseContext, PhaseResult
from devagent.schema import (AcceptanceCheck, ArtifactSpec, Brief, Plan,
                              ProjectScope, Spec, Task)

USAGE = {"tokens_in": 1, "tokens_out": 1}


def _patch_llm(monkeypatch):
    brief = Brief(source="prd", title="Hello", summary="A page", requirements=["show headline"])
    spec = Spec(title="Hello", pages=["/"],
                acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])
    plan = Plan(tasks=[Task(id="t1", description="scaffold", owned_files=["web/src/App.tsx"])])
    scope = ProjectScope(title="Hello", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                     detail={"pages": ["/"]},
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")]),
    ])
    # patch the name bound in each phase module (they did `from ..llm import ...`)
    monkeypatch.setattr("devagent.phases.intake.generate_structured", lambda *a, **k: (brief, USAGE))
    monkeypatch.setattr("devagent.phases.spec.generate_structured", lambda *a, **k: (spec, USAGE))
    # PlanPhase now reads ctx.artifacts["scope"] (M6); the CLI pipeline still wires
    # SpecPhase -> PlanPhase (SpecPhase stores under "spec", not "scope") — ScopePhase
    # replaces this seam in Task 10. For now, patch PlanPhase.run directly so the CLI
    # wiring tests exercise orchestrator/gate logic without a real scope in the context.
    def _fake_plan_run(self, ctx: PhaseContext) -> PhaseResult:
        return PhaseResult(name="plan", exit_code=0, output=f"{len(plan.tasks)} tasks",
                           meta=USAGE, output_artifact=plan)
    monkeypatch.setattr("devagent.phases.plan.PlanPhase.run", _fake_plan_run)


def test_cli_brain_pipeline_succeeds_and_persists_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))
    _patch_llm(monkeypatch)
    rc = cli.main(["run", "examples/hello.md"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "succeeded" in out and "1 tasks" in out

    run_dirs = list(tmp_path.glob("run-*"))
    assert len(run_dirs) == 1
    rd = run_dirs[0]
    assert (rd / "intake.json").is_file()
    assert (rd / "spec.json").is_file()
    assert (rd / "plan.json").is_file()
    text = (rd / "ledger.jsonl").read_text()
    assert '"status": "succeeded"' in text


def test_cli_missing_input_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))
    assert cli.main(["run", "/does/not/exist.md"]) == 2


def test_cli_build_flag_runs_contained_build_end_to_end(tmp_path, monkeypatch, capsys):
    """--build appends a BuildPhase+BuildGate; the executor is swapped for a fake that
    writes the bundle, so the whole PRD->spec->plan->build flow runs without Docker/tokens."""
    import json

    from pathlib import Path

    from devagent.deploy import DeployResult
    from devagent.executor import BuildResult
    from devagent.gates import GateResult
    from devagent.phases.base import PhaseResult
    from devagent.verifier import CheckResult, VerifyReport

    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("DEVAGENT_EGRESS", "0")  # fakes, no real docker network/proxy
    _patch_llm(monkeypatch)

    class FakeSdk:
        def __init__(self, *a, **k):
            pass

        def build(self, req):
            dist = Path(req.workdir) / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "index.html").write_text("<html></html>")
            return BuildResult(repo_path=req.workdir, success=True, tokens_in=10, tokens_out=5)

    class FakeVerifier:
        def __init__(self, *a, **k):
            pass

        def verify(self, req):  # rebuild-from-source + acceptance re-check (no Docker in the test)
            return VerifyReport(build_ok=True, dist_present=True, exit_code=0,
                                checks=[CheckResult("route_status", "/", True, "status 200")])

    class FakeDeployPhase:  # no real preview container in the unit test
        name = "deploy"

        def __init__(self, *a, **k):
            pass

        def run(self, ctx):
            art = DeployResult(url="http://localhost:9999", container="x")
            return PhaseResult("deploy", 0, output=art.url, meta={"url": art.url}, output_artifact=art)

    class FakeDeployGate:
        name = "preview_responds"

        def check(self, result):
            return GateResult(True)

    monkeypatch.setattr("devagent.cli.SdkExecutor", FakeSdk)
    monkeypatch.setattr("devagent.cli.BuildVerifier", FakeVerifier)
    monkeypatch.setattr("devagent.cli.DeployPhase", FakeDeployPhase)
    monkeypatch.setattr("devagent.cli.DeployGate", FakeDeployGate)

    rc = cli.main(["run", "--build", "examples/hello.md"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "succeeded" in out and "built + verified" in out
    assert "preview: http://localhost:9999" in out and "report:" in out

    rd = next(iter(tmp_path.glob("run-*")))
    events = [json.loads(line) for line in (rd / "ledger.jsonl").read_text().splitlines()]
    phases_run = [e["phase"] for e in events if e["event"] == "phase"]
    assert "build" in phases_run and "deploy" in phases_run
    gates_ok = {e["phase"]: e["ok"] for e in events if e["event"] == "gate"}
    assert gates_ok["build"] is True and gates_ok["deploy"] is True
    assert (rd / "out" / "dist" / "index.html").is_file()
    assert (rd / "report.html").is_file()  # run report always written
