"""M7 CLI wiring: publisher wraps run_node and finalizes on success — via fakes,
no Docker/network/LLM. Follows test_cli_build_system.py's monkeypatch style."""
from pathlib import Path
from types import SimpleNamespace

from devagent import cli
from devagent.schema import Contract, ServiceNode, SystemDesign


class FakePub:
    def __init__(self):
        self.wrapped = False
        self.finalized = []

    def wrap(self, run_node):
        self.wrapped = True
        return run_node

    def finalize(self, report, prd_path=None, change_note=None):
        self.finalized.append((getattr(report, "status", None), change_note))


def _fake_report(status):
    return SimpleNamespace(title="t", status=status, build_ok=status == "succeeded",
                           node_results={}, integration=None, urls={}, repairs=[],
                           findings=[])


def _fake_config(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.Config, "load", classmethod(
        lambda c: SimpleNamespace(runs_dir=tmp_path / "runs", max_tokens=1, max_seconds=1,
                                  max_retries=0, max_cost_usd=None, max_system_repairs=1)))


def _wire_build(monkeypatch, tmp_path, status, pub):
    prd = tmp_path / "prd.md"
    prd.write_text("an app")
    _fake_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.GitPublisher, "from_env",
                        classmethod(lambda c, run_dir, **kw: pub))
    monkeypatch.setattr(cli, "make_run_node", lambda *a, **kw: lambda *aa, **kk: None)
    monkeypatch.setattr(cli, "make_bring_up", lambda *a, **kw: lambda design: ({}, None))
    monkeypatch.setattr(cli, "build_system", lambda *a, **kw: _fake_report(status))
    monkeypatch.setattr(cli, "_system_security", lambda ledger: (None, []),
                        raising=False)
    return SimpleNamespace(input=str(prd))


def test_build_system_wraps_and_finalizes_on_success(monkeypatch, tmp_path):
    pub = FakePub()
    args = _wire_build(monkeypatch, tmp_path, "succeeded", pub)
    assert cli._build_system(args) == 0
    assert pub.wrapped and len(pub.finalized) == 1


def test_build_system_skips_finalize_on_failure(monkeypatch, tmp_path):
    pub = FakePub()
    args = _wire_build(monkeypatch, tmp_path, "build_failed", pub)
    assert cli._build_system(args) == 1
    assert pub.wrapped and pub.finalized == []


def test_build_system_unconfigured_env_composes_todays_pipeline(monkeypatch, tmp_path):
    args = _wire_build(monkeypatch, tmp_path, "succeeded", None)   # from_env -> None
    assert cli._build_system(args) == 0                            # no publisher, no crash


def _design_json():
    return SystemDesign(
        title="Todos",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="a JSON API", provides=["api.openapi"])],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}}}})],
    ).model_dump_json(indent=2)


def _wire_update(monkeypatch, tmp_path, status, pub):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "design.json").write_text(_design_json())
    change = tmp_path / "change.md"
    change.write_text("Add dark mode\nsome more detail")
    _fake_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.GitPublisher, "from_env",
                        classmethod(lambda c, run_dir, **kw: pub))
    monkeypatch.setattr(cli, "make_run_node", lambda *a, **kw: lambda *aa, **kk: None)
    monkeypatch.setattr(cli, "make_update_build_service", lambda change_request: None)
    monkeypatch.setattr(cli, "make_bring_up", lambda *a, **kw: lambda design: ({}, None))
    monkeypatch.setattr(cli, "update_system", lambda *a, **kw: _fake_report(status))
    monkeypatch.setattr(cli, "_system_security", lambda ledger: (None, []),
                        raising=False)
    return SimpleNamespace(run_dir=str(run_dir), change=str(change))


def test_update_system_wraps_and_finalizes_with_change_note_on_success(monkeypatch, tmp_path):
    pub = FakePub()
    args = _wire_update(monkeypatch, tmp_path, "succeeded", pub)
    assert cli._update_system(args) == 0
    assert pub.wrapped and pub.finalized == [("succeeded", "Add dark mode")]


def test_update_system_skips_finalize_on_failure(monkeypatch, tmp_path):
    pub = FakePub()
    args = _wire_update(monkeypatch, tmp_path, "integration_failed", pub)
    assert cli._update_system(args) == 1
    assert pub.wrapped and pub.finalized == []


def test_update_system_unconfigured_env_composes_todays_pipeline(monkeypatch, tmp_path):
    args = _wire_update(monkeypatch, tmp_path, "succeeded", None)  # from_env -> None
    assert cli._update_system(args) == 0                           # no publisher, no crash


def test_build_system_passes_repo_url_to_publisher(monkeypatch, tmp_path):
    seen = {}
    pub = FakePub()
    args = _wire_build(monkeypatch, tmp_path, "succeeded", pub)
    monkeypatch.setattr(cli.GitPublisher, "from_env",
                        classmethod(lambda c, run_dir, **kw: (seen.update(kw), pub)[1]))
    args.repo = "https://gitlab.test/team/app.git"
    assert cli._build_system(args) == 0
    assert seen.get("repo_url") == "https://gitlab.test/team/app.git"
