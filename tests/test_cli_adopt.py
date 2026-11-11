"""`devagent adopt` through the CLI. No test reaches Docker, the network or a model: every run
uses --skip-map, egress is off, and the fixture service has no registered recipe, so its
verdict is `unmappable` and nothing is built."""

import json

import pytest

from devagent import egress
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
    assert not (out / "design.json").exists()


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
