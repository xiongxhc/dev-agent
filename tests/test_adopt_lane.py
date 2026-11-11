"""The adopt lane end to end, with the map stage faked.

The properties that matter are safety and honesty. Adopt reads someone's real repository and
produces an artifact other tooling will consume, so: it must never write to the source, it must
never quietly omit a service, and it must never present an unmapped service as if the code had
been read.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from devagent.adopt import Fragment, adopt
from devagent.adopt.prove import PROVEN, UNMAPPABLE, UNPROVEN, Verdict, prove_service

_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>{a}</artifactId>'
        "{body}</project>")
_BOOT = ("<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId>"
         "</plugin></plugins></build>")


def _repo(tmp_path):
    """A tiny two-service fixture repo: one Maven service, one Vite frontend."""
    repo = tmp_path / "src-repo"
    (repo / "api/src/main/java").mkdir(parents=True)
    (repo / "pom.xml").write_text(_POM.format(
        a="root", body="<packaging>pom</packaging><properties><java.version>8</java.version>"
                       "</properties><modules><module>api</module></modules>"))
    (repo / "api/pom.xml").write_text(_POM.format(a="api", body=_BOOT))
    (repo / "api/src/main/java/App.java").write_text("@SpringBootApplication class App {}")
    (repo / "web").mkdir()
    (repo / "web/package.json").write_text(json.dumps(
        {"name": "web", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}))
    (repo / "web/pnpm-lock.yaml").write_text("")
    (repo / "web/node_modules/junk").mkdir(parents=True)
    (repo / "web/node_modules/junk/big.js").write_text("x" * 100)
    return repo


def _unmappable(design, run_dir, **kw):
    return [Verdict(n.name, UNMAPPABLE, "stubbed") for n in design.services]


# ---------------------------------------------------------------- end to end

def test_the_lane_writes_a_run_dir_update_system_can_load(tmp_path):
    """The whole point of the shape: `update-system <adopt-run-dir>` needs no new plumbing."""
    from devagent.design_diff import load_design

    out = tmp_path / "adopt-1"
    result = adopt(_repo(tmp_path), out, prover=_unmappable)
    assert result.status == "adopted"
    for name in ("survey.json", "design.json", "ledger.jsonl", "report.html"):
        assert (out / name).is_file(), name
    design = load_design(out)
    assert {s.name for s in design.services} == {"api", "web"}


def test_the_source_repo_is_never_written_to(tmp_path):
    """Adopt describes code; changing it is a separate step. Confusing the two would edit a repo
    the operator only asked us to read."""
    repo = _repo(tmp_path)
    before = {p.relative_to(repo): p.stat().st_mtime_ns
              for p in repo.rglob("*") if p.is_file()}
    adopt(repo, tmp_path / "adopt-2", prover=_unmappable)
    after = {p.relative_to(repo): p.stat().st_mtime_ns
             for p in repo.rglob("*") if p.is_file()}
    assert before == after, "the source repo was modified"


def test_a_run_dir_inside_the_source_repo_is_refused_before_anything_is_written(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="inside the source repo"):
        adopt(repo, repo / "runs" / "adopt-x", prover=_unmappable)
    assert not (repo / "runs").exists()


def _case_insensitive(tmp_path) -> bool:
    probe = tmp_path / "case-probe"
    probe.mkdir()
    return (tmp_path / "CASE-PROBE").exists()


def test_a_run_dir_inside_the_source_repo_is_refused_whatever_its_case(tmp_path):
    """On a case-insensitive filesystem two spellings name one directory, so the guard compares
    directories on disk, not path strings."""
    if not _case_insensitive(tmp_path):
        pytest.skip("the temp filesystem is case-sensitive")
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="inside the source repo"):
        adopt(repo, tmp_path / "SRC-REPO" / "runs", prover=_unmappable)
    assert not (repo / "runs").exists()


def test_a_source_repo_inside_the_run_dir_is_refused_before_anything_is_written(tmp_path):
    """The copies go under the run dir, so a repo below it would be written into."""
    run = tmp_path / "run"
    repo = _repo(run)
    before = sorted(p.relative_to(run) for p in run.rglob("*"))
    with pytest.raises(ValueError, match="is inside the run dir"):
        adopt(repo, run, prover=_unmappable)
    assert sorted(p.relative_to(run) for p in run.rglob("*")) == before


def _snapshot(root):
    return {p.relative_to(root): (p.is_symlink(), p.read_bytes() if p.is_file() else None,
                                  p.lstat().st_mtime_ns)
            for p in root.rglob("*")}


def test_a_run_dir_that_already_holds_a_run_is_refused_and_left_alone(tmp_path):
    """Copying into a previous run's tree would write through the links it copied, or fail on
    them, so a used run dir is never reused and never cleaned."""
    repo = _repo(tmp_path)
    (repo / "outside-link").symlink_to(tmp_path / "victim")
    (tmp_path / "victim").write_text("untouched")
    out = tmp_path / "out"
    adopt(repo, out, prover=_unmappable)
    before = _snapshot(out)
    (repo / "outside-link").unlink()
    (repo / "outside-link").write_text("repo content")
    with pytest.raises(ValueError, match="already holds a run"):
        adopt(repo, out, prover=_unmappable)
    assert _snapshot(out) == before
    assert (tmp_path / "victim").read_text() == "untouched"


def test_an_existing_empty_run_dir_is_accepted(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = adopt(_repo(tmp_path), out, prover=_unmappable)
    assert result.status == "adopted"
    assert (out / "design.json").is_file()


def test_service_source_is_copied_without_build_output(tmp_path):
    out = tmp_path / "adopt-3"
    adopt(_repo(tmp_path), out, prover=_unmappable)
    assert (out / "services/api/out/pom.xml").is_file()
    assert not (out / "services/web/out/node_modules").exists(), \
        "vendored dependencies must not be adopted as source"


def test_symlinks_are_copied_as_links_and_never_followed(tmp_path):
    """A link in someone's repo can dangle or point back at its own directory. Following it
    either crashes the copy or recurses without end, and a link to somewhere outside the repo
    would pull that tree into the run dir."""
    repo = _repo(tmp_path)
    # The first surveyed service is `api`, a module of the reactor at the repo root, so its
    # copied source is the reactor root: links there land at the top of the copy, and links in
    # the module directory land one level down.
    svc = repo
    (svc / "dangling").symlink_to(svc / "does-not-exist")
    (svc / "loop").symlink_to(svc, target_is_directory=True)
    (svc / "api/dangling").symlink_to(svc / "api/does-not-exist")
    (svc / "api/loop").symlink_to(svc / "api", target_is_directory=True)
    result = adopt(repo, tmp_path / "out", prover=_unmappable)
    assert result.status == "adopted"
    copied = tmp_path / "out" / "services" / result.survey.services[0].name / "out"
    assert (copied / "dangling").is_symlink() and (copied / "loop").is_symlink()
    assert (copied / "api/dangling").is_symlink() and (copied / "api/loop").is_symlink()


def test_a_faked_map_stage_supplies_the_description_and_contract(tmp_path):
    out = tmp_path / "adopt-4"

    def fake_map(repo, service):
        if service.name != "api":
            return None
        return Fragment(service="api", prd_slice="Serves the widget API." * 3, kind="backend",
                        provides=({"id": "api-openapi", "kind": "openapi",
                                   "spec": {"paths": {"/widgets": {}}}},))

    result = adopt(_repo(tmp_path), out, map_fn=fake_map, prover=_unmappable)
    design = result.design
    api = next(s for s in design.services if s.name == "api")
    assert api.provides == ["api-openapi"]
    assert not api.prd_slice.startswith("NOT MAPPED")
    web = next(s for s in design.services if s.name == "web")
    assert web.prd_slice.startswith("NOT MAPPED"), "an unmapped service must say so"


def test_a_map_failure_degrades_only_that_service(tmp_path):
    """One service failing to map is a verdict about that service, not the end of the run."""
    out = tmp_path / "adopt-5"

    def exploding_map(repo, service):
        raise RuntimeError("session died")

    result = adopt(_repo(tmp_path), out, map_fn=exploding_map, prover=_unmappable)
    assert result.status == "adopted"
    assert all(s.prd_slice.startswith("NOT MAPPED") for s in result.design.services)
    events = [json.loads(l) for l in (out / "ledger.jsonl").read_text().splitlines()]
    assert any(e["event"] == "adopt_map_failed" for e in events)


def test_an_empty_repo_is_survey_only_not_a_crash(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    result = adopt(empty, tmp_path / "adopt-6")
    assert result.status == "survey_only"
    assert (tmp_path / "adopt-6" / "report.html").is_file(), "still reviewable"


def test_the_ledger_records_every_stage(tmp_path):
    out = tmp_path / "adopt-7"
    adopt(_repo(tmp_path), out, prover=_unmappable)
    kinds = [json.loads(l)["event"] for l in (out / "ledger.jsonl").read_text().splitlines()]
    for stage in ("adopt_start", "adopt_survey", "adopt_mapped", "adopt_design",
                  "adopt_sources", "adopt_proved", "adopt_end"):
        assert stage in kinds, stage


def test_an_unmappable_service_still_appears_carrying_the_recipe_it_needs(tmp_path):
    """`stack` is required by the schema. Naming the NEEDED recipe keeps the service visible and
    the gap self-documenting, where naming an existing one would make prove attempt it."""
    repo = tmp_path / "toolrepo"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"name": "tool", "scripts": {"build": "tsc"}}))
    (repo / "package-lock.json").write_text("{}")
    result = adopt(repo, tmp_path / "adopt-8", prover=_unmappable)
    node = result.design.services[0]
    assert node.stack == "node-npm"
    from devagent import recipes
    assert node.stack not in recipes.REGISTRY, "it names what is needed, not what exists"


def test_the_lane_hands_prove_each_services_module_path(tmp_path):
    """A reactor module is built from the reactor root by its path; everything else is built
    from its own copied source, which IS the module."""
    repo = tmp_path / "reactor"
    (repo / "services/api/src/main/java").mkdir(parents=True)
    (repo / "pom.xml").write_text(_POM.format(
        a="root", body="<packaging>pom</packaging><properties><java.version>17</java.version>"
                       "</properties><modules><module>services/api</module></modules>"))
    (repo / "services/api/pom.xml").write_text(_POM.format(a="api", body=_BOOT))
    (repo / "services/api/src/main/java/App.java").write_text(
        "@SpringBootApplication class App {}")
    (repo / "web").mkdir()
    (repo / "web/package.json").write_text(json.dumps(
        {"name": "web", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}))
    (repo / "web/yarn.lock").write_text("")
    seen = {}

    def prover(design, run_dir, **kw):
        seen.update(kw.get("modules") or {})
        return []

    adopt(repo, tmp_path / "out", prover=prover)
    assert seen == {"api": "services/api", "web": "."}


def test_a_single_module_repo_hands_prove_its_root(tmp_path):
    repo = tmp_path / "single"
    (repo / "src/main/java").mkdir(parents=True)
    (repo / "pom.xml").write_text(_POM.format(
        a="api", body="<properties><java.version>17</java.version></properties>" + _BOOT))
    (repo / "src/main/java/App.java").write_text("@SpringBootApplication class App {}")
    seen = {}

    def prover(design, run_dir, **kw):
        seen.update(kw.get("modules") or {})
        return []

    adopt(repo, tmp_path / "out", prover=prover)
    assert seen == {"api": "."}


# ---------------------------------------------------------------- prove

def _recipe(build_cmd="mvn -q package", artifact="target/*.jar"):
    return SimpleNamespace(build_cmd=build_cmd, artifact_glob=artifact,
                           toolchain=SimpleNamespace(image="img", egress_hosts=()))


def _node(stack="r", name="api"):
    return SimpleNamespace(name=name, stack=stack)


def _ok(*a):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_prove_reports_unmappable_without_attempting_a_build(tmp_path):
    called = []
    v = prove_service(_node(stack="nope"), tmp_path, registry={},
                      runner=lambda *a: called.append(a))
    assert v.status == UNMAPPABLE and called == [], "nothing should be attempted"


def test_prove_reports_proven_when_the_artifact_appears(tmp_path):
    src = tmp_path / "out"
    (src / "target").mkdir(parents=True)
    (src / "target" / "app.jar").write_text("x")
    v = prove_service(_node(), src, registry={"r": _recipe()}, runner=_ok)
    assert v.status == PROVEN


def test_prove_reports_unproven_when_the_build_succeeds_but_produces_nothing(tmp_path):
    """The artifact is what makes a build real; a green exit code alone is not evidence."""
    src = tmp_path / "out"
    src.mkdir()
    v = prove_service(_node(), src, registry={"r": _recipe()}, runner=_ok)
    assert v.status == UNPROVEN and "absent" in v.detail


def test_prove_survives_a_broken_toolchain(tmp_path):
    src = tmp_path / "out"
    src.mkdir()

    def boom(*a):
        raise OSError("no docker")

    v = prove_service(_node(), src, registry={"r": _recipe()}, runner=boom)
    assert v.status == UNPROVEN and "could not run" in v.detail


_MODULE_RECIPE = _recipe(build_cmd="mvn -pl {module} -am package",
                         artifact="{module}/target/*.jar")


def _recording(commands):
    def runner(image, source, build_cmd, *a):
        commands.append(build_cmd)
        return _ok()
    return runner


def test_a_single_module_maven_service_builds_from_its_root(tmp_path):
    src = tmp_path / "out"
    (src / "target").mkdir(parents=True)
    (src / "target/app.jar").write_text("x")
    commands = []
    v = prove_service(_node(), src, registry={"r": _MODULE_RECIPE}, runner=_recording(commands))
    assert commands and "-pl . -am" in commands[0]
    assert v.status == PROVEN, v.detail


def test_a_reactor_module_builds_by_its_path_not_its_name(tmp_path):
    src = tmp_path / "out"
    (src / "services/api/target").mkdir(parents=True)
    (src / "services/api/target/api.jar").write_text("x")
    commands = []
    v = prove_service(_node(name="api"), src, module="services/api",
                      registry={"r": _MODULE_RECIPE}, runner=_recording(commands))
    assert commands and "-pl services/api -am" in commands[0]
    assert v.status == PROVEN, v.detail


@pytest.mark.parametrize("module", ["../shared", "/abs/api", "services/../../x"])
def test_a_module_outside_the_copied_source_is_unproven_and_nothing_runs(tmp_path, module):
    src = tmp_path / "out"
    src.mkdir()
    (src / "pom.xml").write_text("<project/>")
    commands = []
    v = prove_service(_node(), src, module=module, registry={"r": _MODULE_RECIPE},
                      runner=_recording(commands))
    assert v.status == UNPROVEN and "outside" in v.detail
    assert commands == [], "a build against a path outside the copy must not run"


def test_a_module_path_reaches_the_shell_as_one_quoted_word(tmp_path):
    """The module path is repo text inside a `sh -c` command; quoting keeps it one argument and
    stops the shell expanding it."""
    module = "my mod/$(echo x)"
    src = tmp_path / "out"
    (src / module / "target").mkdir(parents=True)
    (src / module / "target/app.jar").write_text("x")
    commands = []
    v = prove_service(_node(), src, module=module, registry={"r": _MODULE_RECIPE},
                      runner=_recording(commands))
    assert commands == ["mvn -pl 'my mod/$(echo x)' -am package"]
    assert v.status == PROVEN, v.detail


@pytest.mark.parametrize("module", ["", "   "])
def test_an_empty_module_path_is_the_copied_source_root(tmp_path, module):
    src = tmp_path / "out"
    (src / "target").mkdir(parents=True)
    (src / "target/app.jar").write_text("x")
    commands = []
    v = prove_service(_node(), src, module=module, registry={"r": _MODULE_RECIPE},
                      runner=_recording(commands))
    assert commands and "-pl . -am" in commands[0]
    assert v.status == PROVEN, v.detail


def test_prove_passes_each_service_its_module(tmp_path):
    """`prove` reads the module for each service from the map; a name the map lacks is `.`."""
    from devagent.adopt.prove import prove

    for name, rel in (("api", "services/api/target"), ("web", "target")):
        (tmp_path / "services" / name / "out" / rel).mkdir(parents=True)
        (tmp_path / "services" / name / "out" / rel / "app.jar").write_text("x")
    design = SimpleNamespace(services=[_node(name="api"), _node(name="web")])
    commands = []
    verdicts = prove(design, tmp_path, modules={"api": "services/api"},
                     registry={"r": _MODULE_RECIPE}, runner=_recording(commands))
    assert [v.status for v in verdicts] == [PROVEN, PROVEN]
    assert ["-pl services/api -am" in commands[0], "-pl . -am" in commands[1]] == [True, True]


# ---------------------------------------------------------------- report

def test_the_report_leads_with_what_is_not_known(tmp_path):
    out = tmp_path / "adopt-9"
    adopt(_repo(tmp_path), out, prover=_unmappable)
    html = (out / "report.html").read_text()
    assert "not truth until a human says so" in html
    assert "were not mapped" in html


# ---------------------------------------------------------------- one system, several repos
#
# A system can span repositories: a Spring Boot backend in one and a frontend in another.
# Adopting either alone produces a design whose frontend consumes an API nothing in it
# provides. Merging the surveys is what makes the wiring gate meaningful rather than an
# obstacle to work around.

def _two_repos(tmp_path):
    api = tmp_path / "repo-api"
    (api / "src/main/java").mkdir(parents=True)
    (api / "pom.xml").write_text(_POM.format(
        a="api", body="<properties><java.version>8</java.version></properties>" + _BOOT))
    (api / "src/main/java/App.java").write_text("@SpringBootApplication class App {}")
    (api / "Dockerfile").write_text("FROM eclipse-temurin:8-jre")
    web = tmp_path / "repo-web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps(
        {"name": "web", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}))
    (web / "pnpm-lock.yaml").write_text("")
    (web / "Dockerfile").write_text("FROM node:20-slim")
    return api, web


def test_several_repos_survey_as_one_system(tmp_path):
    from devagent.adopt.survey import survey_repos

    api, web = _two_repos(tmp_path)
    merged = survey_repos([api, web])
    assert {s.name for s in merged.services} == {"api", "web"}
    assert {Path(s.repo).name for s in merged.services} == {"repo-api", "repo-web"}


def test_a_cross_repo_contract_wires_up_when_both_repos_are_adopted(tmp_path):
    """Mapped alone, the frontend's consume is a dependency on something outside the design."""
    api, web = _two_repos(tmp_path)

    def fake_map(repo, service):
        if service.name == "api":
            return Fragment(service="api", prd_slice="Serves the admin API for the console." * 2,
                            kind="backend",
                            provides=({"id": "api-openapi", "kind": "openapi", "spec": {}},))
        return Fragment(service="web", prd_slice="Admin console calling the api." * 2,
                        kind="frontend", consumes=("api-openapi",))

    # Adopted alone, the frontend's dependency on the other repo is recorded as external —
    # true, and reviewable, rather than a refusal.
    alone = adopt(web, tmp_path / "one", map_fn=fake_map, prover=_unmappable)
    assert alone.status == "adopted"
    assert [e["contract"] for e in alone.externals] == ["api-openapi"]

    together = adopt([api, web], tmp_path / "both", map_fn=fake_map, prover=_unmappable)
    assert together.status == "adopted", together.detail
    assert [c.id for c in together.design.contracts] == ["api-openapi"]
    web_node = next(s for s in together.design.services if s.name == "web")
    assert web_node.consumes == ["api-openapi"], "adopted together, it is intra-system wiring"
    assert web_node.depends_on == ["api"], "the dependency edge is derived from the contract"
    assert together.externals == [], "nothing is external once both repos are adopted"


def test_source_is_copied_from_each_service_own_repo(tmp_path):
    api, web = _two_repos(tmp_path)
    out = tmp_path / "both-2"
    adopt([api, web], out, prover=_unmappable)
    assert (out / "services/api/out/pom.xml").is_file()
    assert (out / "services/web/out/package.json").is_file()


def test_a_duplicate_service_name_across_repos_is_refused(tmp_path):
    """Service ids must be unique in a design, so the collision has to be named before adoption
    rather than producing a design that silently describes one of them twice."""
    from devagent.adopt.survey import survey_repos

    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "package.json").write_text(json.dumps({"name": "web", "scripts": {"build": "x"}}))
        (d / "pnpm-lock.yaml").write_text("")
    with pytest.raises(ValueError, match="appears in both"):
        survey_repos([a, b])


def test_notes_say_which_repo_they_came_from(tmp_path):
    """A merged survey's notes are useless if you cannot tell which repo each describes."""
    from devagent.adopt.survey import survey_repos

    api, web = _two_repos(tmp_path)
    merged = survey_repos([api, web])
    assert any(n.startswith("[repo-api]") for n in merged.notes)
    assert any(n.startswith("[repo-web]") for n in merged.notes)


def test_a_single_repo_still_works_unchanged(tmp_path):
    api, _ = _two_repos(tmp_path)
    result = adopt(api, tmp_path / "single", prover=_unmappable)
    assert result.status == "adopted" and len(result.design.services) == 1


def test_a_survey_that_raises_leaves_no_run_dir(tmp_path):
    """Two services with one name abort the survey; nothing may have been created by then."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "package.json").write_text(json.dumps({"name": "web", "scripts": {"build": "x"}}))
        (d / "pnpm-lock.yaml").write_text("")
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="appears in both"):
        adopt([a, b], out, prover=_unmappable)
    assert not out.exists()


def test_check_run_dir_refuses_what_adopt_refuses(tmp_path):
    from devagent.adopt.lane import check_run_dir

    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="inside the source repo"):
        check_run_dir([repo], repo / "runs" / "x")
    used = tmp_path / "used"
    used.mkdir()
    (used / "f").write_text("x")
    with pytest.raises(ValueError, match="already holds a run"):
        check_run_dir([repo], used)
    check_run_dir([repo], tmp_path / "fresh")
    assert not (tmp_path / "fresh").exists()
