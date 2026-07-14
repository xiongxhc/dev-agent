import json
from pathlib import Path

from devagent import cli
from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.system_build import SystemReport


def _design_json():
    return SystemDesign(
        title="Todos",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="a JSON API", provides=["api.openapi"])],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}}}})],
    ).model_dump_json(indent=2)


def test_update_system_requires_a_prior_design(tmp_path):
    change = tmp_path / "c.md"
    change.write_text("x")
    assert cli.main(["update-system", str(tmp_path), str(change)]) == 2


def test_update_system_requires_the_change_file(tmp_path):
    (tmp_path / "design.json").write_text(_design_json())
    assert cli.main(["update-system", str(tmp_path), str(tmp_path / "missing.md")]) == 2


def test_update_system_dispatches_and_writes_report(tmp_path, monkeypatch):
    (tmp_path / "design.json").write_text(_design_json())
    change = tmp_path / "c.md"
    change.write_text("add dark mode")
    seen = {}

    def fake_update_system(run_dir, change_path, **kw):
        seen["run_dir"] = Path(run_dir)
        seen["kw"] = kw
        return SystemReport("Todos", {}, True, None, "succeeded",
                            urls={"api": "http://localhost:1"})

    monkeypatch.setattr(cli, "update_system", fake_update_system)
    rc = cli.main(["update-system", str(tmp_path), str(change)])
    assert rc == 0 and seen["run_dir"] == tmp_path
    assert callable(seen["kw"]["bring_up_factory"])
    assert callable(seen["kw"]["run_node"])
    assert seen["kw"]["security_verify"] is not None
    report = json.loads((tmp_path / "system-report.json").read_text())
    assert report["status"] == "succeeded" and report["urls"] == {"api": "http://localhost:1"}
    # the update appended to the run's EXISTING ledger (event trail continues)
    assert any(json.loads(l).get("event") == "update_input"
               for l in (tmp_path / "ledger.jsonl").read_text().splitlines())


def test_update_system_nonsuccess_exits_1(tmp_path, monkeypatch):
    (tmp_path / "design.json").write_text(_design_json())
    change = tmp_path / "c.md"
    change.write_text("x")
    monkeypatch.setattr(cli, "update_system",
                        lambda *a, **kw: SystemReport("t", {}, True, None, "integration_failed"))
    assert cli.main(["update-system", str(tmp_path), str(change)]) == 1
