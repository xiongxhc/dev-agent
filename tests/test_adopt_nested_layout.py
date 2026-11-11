"""A repo that keeps a Maven `backend/` reactor beside a node `frontend/`.

The node walk descends one level; a Maven walk that read only `<repo>/pom.xml` would report a
complete-looking adoption of the frontend alone, with the backend appearing nowhere: not as a
service, not as a library, not as a gap. A wrong answer that looks right is the worst outcome
for a lane whose whole product is a reviewable description, so this pins the shape.
"""

import json

from devagent.adopt import adopt
from devagent.adopt.prove import UNPROVEN, Verdict
from devagent.adopt.survey import survey_repo

_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>{a}</artifactId>'
        "{body}</project>")
_BOOT = ("<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId>"
         "</plugin></plugins></build>")


def _nested_repo(tmp_path, name="app"):
    """backend/ = a reactor with one boot service and one library; frontend/ = a vite+yarn app
    whose package name differs from its directory."""
    repo = tmp_path / name
    (repo / "backend").mkdir(parents=True)
    (repo / "backend/pom.xml").write_text(_POM.format(
        a="backend-parent",
        body="<packaging>pom</packaging><properties><java.version>8</java.version></properties>"
             "<modules><module>api</module><module>lib-common</module></modules>"))
    (repo / "backend/api/src/main/java").mkdir(parents=True)
    (repo / "backend/api/pom.xml").write_text(_POM.format(a="api", body=_BOOT))
    (repo / "backend/api/src/main/java/App.java").write_text(
        "@SpringBootApplication class App {}")
    (repo / "backend/lib-common").mkdir()
    (repo / "backend/lib-common/pom.xml").write_text(_POM.format(a="lib-common", body=""))
    (repo / "frontend").mkdir()
    (repo / "frontend/package.json").write_text(json.dumps(
        {"name": "web", "scripts": {"build": "vite build"},
         "devDependencies": {"vite": "5"}}))
    (repo / "frontend/yarn.lock").write_text("")
    return repo


def test_the_maven_backend_is_found_from_the_repo_root(tmp_path):
    survey = survey_repo(_nested_repo(tmp_path))
    assert sorted(s.name for s in survey.services) == ["api", "web"]
    assert [l.name for l in survey.libraries] == ["lib-common"]


def test_a_sub_reactor_records_itself_as_the_build_root(tmp_path):
    """`mvn -pl api -am` only works from the reactor. If the module recorded the repo root
    instead, the copied source would have no pom.xml at its top and every build would fail."""
    repo = _nested_repo(tmp_path)
    svc = next(s for s in survey_repo(repo).services if s.name == "api")
    assert svc.repo == str(repo / "backend")
    assert svc.path == "api"


def test_a_node_root_still_records_the_repo(tmp_path):
    repo = _nested_repo(tmp_path)
    web = next(s for s in survey_repo(repo).services if s.name == "web")
    assert (web.repo, web.path) == (str(repo), "frontend")


def test_a_root_level_reactor_is_unchanged(tmp_path):
    """A reactor adopted directly at its own root: the path that already worked must not
    shift."""
    repo = tmp_path / "backend"
    (repo / "api/src/main/java").mkdir(parents=True)
    (repo / "pom.xml").write_text(_POM.format(
        a="parent", body="<packaging>pom</packaging><modules><module>api</module></modules>"))
    (repo / "api/pom.xml").write_text(_POM.format(a="api", body=_BOOT))
    (repo / "api/src/main/java/App.java").write_text("@SpringBootApplication class App {}")
    svc = survey_repo(repo).services[0]
    assert (svc.name, svc.repo, svc.path) == ("api", str(repo), "api")


def test_the_reactor_note_says_which_reactor(tmp_path):
    """Two reactors in one repo make an unlabelled 'maven parent packaging=pom' note useless."""
    notes = " ".join(survey_repo(_nested_repo(tmp_path)).notes)
    assert "backend: maven parent packaging=pom" in notes


def test_adopt_copies_the_reactor_not_the_repo(tmp_path):
    """The source copy keys on the same reactor root, so the adopted tree is buildable: a pom at
    its top, and no unrelated sibling dragged along."""
    repo = _nested_repo(tmp_path)
    out = tmp_path / "out"
    adopt(repo, out, prover=lambda d, rd, **kw: [])
    assert (out / "services/api/out/pom.xml").is_file()
    assert (out / "services/api/out/api/pom.xml").is_file()
    assert not (out / "services/api/out/frontend").exists()
    assert (out / "services/web/out/package.json").is_file()


def test_a_whole_nested_repo_adopts_as_one_system(tmp_path):
    """What the operator actually types: `adopt <repo>`. Both halves, one design."""
    repo = _nested_repo(tmp_path)
    out = tmp_path / "out"
    result = adopt(repo, out, prover=lambda d, rd, **kw: [])
    assert result.status == "adopted"
    assert sorted(s.name for s in result.design.services) == ["api", "web"]


# ------------------------------------------------------- an unproven build must be actionable

def test_a_failed_build_keeps_its_log(tmp_path):
    """The build output is the only thing that makes an unproven verdict actionable: without it
    the report says "build exited 1" and the operator has nowhere to look."""
    repo = _nested_repo(tmp_path)
    out = tmp_path / "out"
    tail = "ERROR: Cannot find module 'concurrently'\nexit 1\n"
    adopt(repo, out, prover=lambda d, rd, **kw: [
        Verdict("web", UNPROVEN, "build exited 1", tail)])

    log = out / "services/web/build.log"
    assert log.read_text() == tail
    events = [json.loads(l) for l in (out / "ledger.jsonl").read_text().splitlines()]
    proved = next(e for e in events if e["event"] == "adopt_proved")
    assert proved["log"] == "services/web/build.log"
    assert "services/web/build.log" in (out / "report.html").read_text()


def test_a_verdict_with_no_log_writes_none(tmp_path):
    """An unmappable service never ran a build; an empty build.log would imply it did."""
    repo = _nested_repo(tmp_path)
    out = tmp_path / "out"
    adopt(repo, out, prover=lambda d, rd, **kw: [
        Verdict("web", "unmappable", "no registered recipe")])
    assert not (out / "services/web/build.log").exists()
    assert "build log" not in (out / "report.html").read_text()


# ------------------------------------------------- a mount the daemon cannot see

def test_an_unshared_run_dir_is_reported_as_such_and_no_build_runs(tmp_path):
    """On a VM-backed Docker a run dir outside the shared paths mounts as an empty directory,
    and the build then fails with a log that blames the code (a lockfile "not found", a
    directory that cannot be created). The verdict has to name the mount, and nothing should be
    built into a void."""
    from devagent.adopt.prove import prove_service
    from devagent.schema import ServiceNode

    src = tmp_path / "out"
    src.mkdir()
    (src / "package.json").write_text("{}")
    node = ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-yarn",
                       prd_slice="x", provides=[], consumes=[], depends_on=[])
    built = []
    verdict = prove_service(node, src,
                            runner=lambda *a, **k: built.append(a) or None,
                            mount_check=lambda image, source: "not shared with the daemon")
    assert verdict.status == UNPROVEN
    assert verdict.detail == "not shared with the daemon"
    assert built == [], "a build into an empty mount proves nothing and must not run"


def test_a_shared_run_dir_builds_normally(tmp_path):
    from types import SimpleNamespace

    from devagent.adopt.prove import PROVEN, prove_service
    from devagent.schema import ServiceNode

    src = tmp_path / "out"
    (src / "dist").mkdir(parents=True)
    (src / "dist/index.html").write_text("<html>")
    node = ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-yarn",
                       prd_slice="x", provides=[], consumes=[], depends_on=[])
    verdict = prove_service(node, src,
                            runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
                            mount_check=lambda image, source: None)
    assert verdict.status == PROVEN


def test_a_node_service_inside_the_reactor_is_found_from_the_repo_root(tmp_path):
    """A node service kept inside the Maven reactor directory, beside the modules. Missing it
    would be the same silent omission as missing the backend, one level deeper."""
    repo = _nested_repo(tmp_path)
    (repo / "backend/worker").mkdir()
    (repo / "backend/worker/package.json").write_text(json.dumps(
        {"name": "worker", "scripts": {"build": "tsc"},
         "dependencies": {"typescript": "5.4.0"}}))
    (repo / "backend/worker/package-lock.json").write_text("{}")

    survey = survey_repo(repo)
    worker = next(s for s in survey.services if s.name == "worker")
    assert (worker.repo, worker.path) == (str(repo), "backend/worker")
    assert worker.stack is None and "no registered recipe" in worker.gap
    assert worker.gap in survey.gaps, "a service with no recipe must appear in the gap list"


def test_the_repos_own_package_json_still_wins_over_a_child_walk(tmp_path):
    """A single-app repo must not also adopt its own subdirectories."""
    repo = tmp_path / "solo"
    (repo / "packages/ui").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps(
        {"name": "solo", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}))
    (repo / "yarn.lock").write_text("")
    (repo / "packages/ui/package.json").write_text(json.dumps({"name": "ui"}))
    assert [s.name for s in survey_repo(repo).services] == ["solo"]
