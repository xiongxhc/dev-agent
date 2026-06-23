"""CLI wiring of the brain pipeline — no live tokens (the phases' LLM call is patched)."""

from devagent import cli
from devagent.schema import AcceptanceCheck, Brief, Plan, Spec, Task

USAGE = {"tokens_in": 1, "tokens_out": 1}


def _patch_llm(monkeypatch):
    brief = Brief(source="prd", title="Hello", summary="A page", requirements=["show headline"])
    spec = Spec(title="Hello", pages=["/"],
                acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])
    plan = Plan(tasks=[Task(id="t1", description="scaffold", owned_files=["src/App.tsx"])])
    # patch the name bound in each phase module (they did `from ..llm import ...`)
    monkeypatch.setattr("devagent.phases.intake.generate_structured", lambda *a, **k: (brief, USAGE))
    monkeypatch.setattr("devagent.phases.spec.generate_structured", lambda *a, **k: (spec, USAGE))
    monkeypatch.setattr("devagent.phases.plan.generate_structured", lambda *a, **k: (plan, USAGE))


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
