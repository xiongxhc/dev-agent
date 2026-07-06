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


def test_build_system_non_succeeded_report_returns_1(tmp_path, monkeypatch, capsys):
    prd = tmp_path / "prd.md"; prd.write_text("A todo system.")
    from devagent.system_build import SystemReport
    from devagent.tree import NodeResult, FAILED
    def fake_build_system(prd_path, **kw):
        return SystemReport("Todo", {"api": NodeResult("api", FAILED, "aborted_budget")},
                            False, None, "build_failed")
    monkeypatch.setattr(cli, "build_system", fake_build_system, raising=False)
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path / "runs"))
    rc = cli.main(["build-system", str(prd)])
    assert rc == 1
    reports = list((tmp_path / "runs").rglob("system-report.json"))
    services = json.loads(reports[0].read_text())["services"]
    assert services["api"]["status"] == "failed"
    assert services["api"]["detail"] == "aborted_budget"
    assert "aborted_budget" in capsys.readouterr().out


def test_build_system_report_includes_integration_steps(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"; prd.write_text("A todo system.")
    from devagent.system_build import SystemReport
    from devagent.tree import NodeResult, SUCCEEDED
    from devagent.integration import IntegrationReport
    steps = [{"service": "api", "route": "/api/todos", "ok": True, "detail": ""}]
    def fake_build_system(prd_path, **kw):
        return SystemReport("Todo", {"api": NodeResult("api", SUCCEEDED)}, True,
                            IntegrationReport(steps=steps), "succeeded")
    monkeypatch.setattr(cli, "build_system", fake_build_system, raising=False)
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path / "runs"))
    rc = cli.main(["build-system", str(prd)])
    assert rc == 0
    reports = list((tmp_path / "runs").rglob("system-report.json"))
    assert json.loads(reports[0].read_text())["integration"] == steps


def test_cli_build_system_passes_cap_and_persists_repairs(tmp_path, monkeypatch):
    import json
    from devagent import cli
    from devagent.config import Config
    from devagent.system_build import SystemReport
    from devagent.tree import NodeResult, SUCCEEDED

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(
        runs_dir=tmp_path, max_system_repairs=2)))

    seen = {}
    def fake_build_system(prd, **kw):
        seen.update(kw)
        return SystemReport("t", {"api": NodeResult("api", SUCCEEDED)}, True, None,
                            "succeeded", urls={"api": "http://api"},
                            repairs=[{"attempt": 1, "nodes": ["api"], "outcomes": [],
                                      "integration_ok": True}])
    monkeypatch.setattr(cli, "build_system", fake_build_system)

    prd = tmp_path / "prd.md"; prd.write_text("build me")
    rc = cli._build_system(type("A", (), {"input": str(prd)})())
    assert rc == 0
    assert seen["max_system_repairs"] == 2
    report = json.loads(next(tmp_path.glob("run-*/system-report.json")).read_text())
    assert report["repairs"][0]["nodes"] == ["api"]
