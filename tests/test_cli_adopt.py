"""`devagent adopt` through the CLI. No test reaches Docker, the network or a model. The fixture
service has no registered recipe, so its verdict is `unmappable` and nothing is built; egress is
off unless a test fakes `egress.ensure`. Where mapping is not skipped, the mapper is faked, or
the test sets up an unknown arm or a missing key so `make_mapper` refuses before any session
starts."""

import json

import pytest

from devagent import egress
from devagent.adopt import mapper
from devagent.cli import main


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    monkeypatch.setenv("DEVAGENT_EGRESS", "0")


def _app(path, name="web"):
    """A react-scripts app with an npm lockfile: surveyed as a service, matched by no recipe."""
    path.mkdir(parents=True)
    (path / "package.json").write_text(json.dumps(
        {"name": name, "scripts": {"build": "react-scripts build"},
         "dependencies": {"react-scripts": "5.0.1"}}))
    (path / "package-lock.json").write_text("{}")
    return path


def test_adopt_writes_a_reviewable_run_dir(tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map", "--out", str(out)]) == 0
    for f in ("survey.json", "design.json", "ledger.jsonl", "report.html"):
        assert (out / f).is_file()
    stdout = capsys.readouterr().out
    assert "unmappable 1" in stdout
    assert "web=unmappable" in stdout
    assert str(out / "report.html") in stdout


def test_adopt_refuses_a_path_that_is_not_a_directory(tmp_path, capsys):
    assert main(["adopt", str(tmp_path / "missing"), "--skip-map"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_adopt_refuses_a_run_dir_inside_the_repo(tmp_path, capsys):
    repo = _app(tmp_path / "web")
    assert main(["adopt", str(repo), "--skip-map", "--out", str(repo / "runs" / "x")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "inside the source repo" in err
    assert not (repo / "runs").exists()


def test_adopt_of_a_repo_with_no_services_exits_1(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    out = tmp_path / "out"
    assert main(["adopt", str(tmp_path / "empty"), "--skip-map", "--out", str(out)]) == 1
    assert "no services" in capsys.readouterr().out


def test_the_default_run_dir_is_under_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path / "runs"))
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map"]) == 0
    assert len(list((tmp_path / "runs").glob("adopt-*"))) == 1


def test_adopt_refuses_a_non_empty_run_dir_and_leaves_it_unchanged(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map", "--out", str(out)]) == 2
    assert "already holds a run" in capsys.readouterr().err
    assert [p.name for p in out.iterdir()] == ["keep.txt"]
    assert (out / "keep.txt").read_text() == "mine"


def test_adopt_refuses_two_services_with_one_name(tmp_path, capsys):
    a, b = _app(tmp_path / "a" / "web"), _app(tmp_path / "b" / "web")
    out = tmp_path / "out"
    assert main(["adopt", str(a), str(b), "--skip-map", "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert str(a) in err and str(b) in err
    assert not out.exists()


@pytest.mark.parametrize("exc", [RuntimeError("docker is not running"),
                                 TimeoutError("proxy did not come up")])
def test_adopt_reports_a_failed_egress_proxy_without_a_traceback(tmp_path, monkeypatch, capsys,
                                                                  exc):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")

    def boom(**kw):
        raise exc
    monkeypatch.setattr(egress, "ensure", boom)
    out = tmp_path / "out"
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map", "--out", str(out)]) == 2
    assert f"error: {exc}" in capsys.readouterr().err
    assert not out.exists()


def test_adopt_passes_the_survey_hosts_to_the_egress_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")
    seen = {}

    def fake_ensure(**kw):
        seen.update(kw)
        return "net", "http://proxy:3128"
    monkeypatch.setattr(egress, "ensure", fake_ensure)
    repo = _app(tmp_path / "web")
    (repo / ".npmrc").write_text("registry=https://registry.example.com/\n")
    assert main(["adopt", str(repo), "--skip-map", "--out", str(tmp_path / "out")]) == 0
    assert "registry.example.com" in seen["extra_hosts"]


def _ensure_must_not_run(monkeypatch):
    def fail(**kw):
        raise AssertionError("the egress proxy was prepared before a refusal")
    monkeypatch.setattr(egress, "ensure", fail)


def test_an_unknown_arm_exits_2_before_the_proxy_or_the_run_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")
    monkeypatch.setenv("DEVAGENT_ADOPT_ARM", "bogus")
    _ensure_must_not_run(monkeypatch)
    out = tmp_path / "out"
    assert main(["adopt", str(_app(tmp_path / "web")), "--out", str(out)]) == 2
    assert "unknown adopt arm" in capsys.readouterr().err
    assert not out.exists()


def test_a_missing_key_exits_2_naming_the_variable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")
    monkeypatch.delenv("DEVAGENT_ADOPT_ARM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _ensure_must_not_run(monkeypatch)
    out = tmp_path / "out"
    assert main(["adopt", str(_app(tmp_path / "web")), "--out", str(out)]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not out.exists()


def test_a_refused_run_dir_exits_2_before_the_proxy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")
    _ensure_must_not_run(monkeypatch)
    repo = _app(tmp_path / "web")
    assert main(["adopt", str(repo), "--skip-map", "--out", str(repo / "r")]) == 2
    assert "inside the source repo" in capsys.readouterr().err


def test_the_mapper_gets_the_network_and_proxy_the_cli_prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")
    monkeypatch.setattr(egress, "ensure", lambda **kw: ("net", "http://proxy:3128"))
    seen = {}

    def fake_make_mapper(**kw):
        seen.update(kw)
        return lambda repo, svc: None
    monkeypatch.setattr(mapper, "make_mapper", fake_make_mapper)
    assert main(["adopt", str(_app(tmp_path / "web")), "--out", str(tmp_path / "out")]) == 0
    assert seen["network"] == "net" and seen["proxy_url"] == "http://proxy:3128"


def test_skip_map_never_builds_a_mapper(tmp_path, monkeypatch):
    def fail(**kw):
        raise AssertionError("make_mapper called under --skip-map")
    monkeypatch.setattr(mapper, "make_mapper", fail)
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map",
                 "--out", str(tmp_path / "out")]) == 0


def test_a_failed_gate_exits_1_and_names_the_failure(tmp_path, monkeypatch, capsys):
    from devagent.adopt import assemble

    def boom(*a, **kw):
        raise assemble.GateFailure("contract x has no producing service")
    monkeypatch.setattr(assemble, "compose", boom)
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map",
                 "--out", str(tmp_path / "out")]) == 1
    out = capsys.readouterr().out
    assert "gate_failed" in out and "no producing service" in out
    assert "web" in out and "no services" not in out


def test_adopt_reports_missing_docker_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVAGENT_EGRESS", "1")

    def no_docker(**kw):
        raise FileNotFoundError(2, "No such file or directory", "docker")
    monkeypatch.setattr(egress, "ensure", no_docker)
    assert main(["adopt", str(_app(tmp_path / "web")), "--skip-map",
                 "--out", str(tmp_path / "out")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: docker was not found") and "DEVAGENT_EGRESS=0" not in err
