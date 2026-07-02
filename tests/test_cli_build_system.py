import json
from devagent import cli


def test_build_system_writes_report_and_returns_status(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"; prd.write_text("A todo system.")
    # Patch build_system to avoid Docker/LLM; assert the CLI wires + persists it.
    from devagent.system_build import SystemReport
    from devagent.tree import NodeResult, SUCCEEDED
    def fake_build_system(prd_path, **kw):
        return SystemReport("Todo", {"api": NodeResult("api", SUCCEEDED)}, True, None, "succeeded")
    monkeypatch.setattr(cli, "build_system", fake_build_system, raising=False)
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path / "runs"))
    rc = cli.main(["build-system", str(prd)])
    assert rc == 0
    reports = list((tmp_path / "runs").rglob("system-report.json"))
    assert reports and json.loads(reports[0].read_text())["status"] == "succeeded"
