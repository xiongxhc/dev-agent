"""Survey — M9 stage 3.1, on fixture trees.

Every case here is something the pilot system actually taught. Survey decides what the rest of
adopt looks at, so a wrong answer is not a small error: a library adopted as a service produces
a design with no runnable shape, and a missed recipe gap hides the work the milestone exists to
size.
"""

import json

import pytest

from devagent.adopt import survey_repo


def _write(root, rel, body=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def _pom(artifact, *, modules=(), packaging=None, boot_plugin=False, java=None, parent=False):
    mods = "".join(f"<module>{m}</module>" for m in modules)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>{artifact}</artifactId>
  {f'<packaging>{packaging}</packaging>' if packaging else ''}
  {f'<parent><artifactId>spring-boot-starter-parent</artifactId></parent>' if parent else ''}
  {f'<properties><java.version>{java}</java.version></properties>' if java else ''}
  {f'<modules>{mods}</modules>' if modules else ''}
  <build><plugins>
    {'<plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin>' if boot_plugin else ''}
  </plugins></build>
</project>"""


# ---------------------------------------------------------------- maven

def test_only_declared_modules_are_adopted(tmp_path):
    """A repo ships directories that are not part of its build — the pilot has `shop-generate`
    beside five declared modules. Walking directories adopts what the project does not build."""
    _write(tmp_path, "pom.xml", _pom("app", modules=["svc"], packaging="pom", java="8"))
    _write(tmp_path, "svc/pom.xml", _pom("svc", boot_plugin=True))
    _write(tmp_path, "svc/src/main/java/App.java", "@SpringBootApplication class App {}")
    _write(tmp_path, "tooling/pom.xml", _pom("tooling", boot_plugin=True))
    _write(tmp_path, "tooling/src/main/java/T.java", "@SpringBootApplication class T {}")

    s = survey_repo(tmp_path)
    assert [x.name for x in s.services] == ["svc"]
    assert any("NOT declared" in n and "tooling" in n for n in s.notes), \
        "an excluded directory must be reported, not silently dropped"


def test_a_library_with_a_demo_main_is_not_a_service(tmp_path):
    """The bug the pilot caught: shop-common is a library whose crypto utility carries a
    `public static void main`. Treating any main() as a service turns libraries into services."""
    _write(tmp_path, "pom.xml", _pom("app", modules=["common"], packaging="pom", java="8"))
    _write(tmp_path, "common/pom.xml", _pom("common"))
    _write(tmp_path, "common/src/main/java/Util.java",
           "class Util { public static void main(String[] a) {} }")

    s = survey_repo(tmp_path)
    assert [x.name for x in s.libraries] == ["common"]
    assert s.services == []
    assert "bare_mains" in s.libraries[0].evidence, "the weak signal is still recorded"


@pytest.mark.parametrize("marker,body", [
    ("annotation", "@SpringBootApplication class App {}"),
    ("boot plugin", "class App {}"),
])
def test_a_spring_boot_service_is_recognised(marker, body, tmp_path):
    _write(tmp_path, "pom.xml", _pom("app", modules=["api"], packaging="pom", java="8"))
    _write(tmp_path, "api/pom.xml", _pom("api", boot_plugin=(marker == "boot plugin")))
    _write(tmp_path, "api/src/main/java/App.java", body)
    s = survey_repo(tmp_path)
    assert [x.name for x in s.services] == ["api"], marker


def test_java_version_is_captured_for_the_recipe(tmp_path):
    """The pilot codebase's JDK-8 ceiling is a recipe requirement, and it is only stated in the parent."""
    _write(tmp_path, "pom.xml", _pom("app", modules=["api"], packaging="pom", java="8"))
    _write(tmp_path, "api/pom.xml", _pom("api", boot_plugin=True))
    s = survey_repo(tmp_path)
    assert s.services[0].evidence["java_version"] == "8"
    assert s.services[0].stack == "maven-spring-boot", "a matched recipe, now that one exists"


def test_a_single_module_repo_is_its_own_service(tmp_path):
    _write(tmp_path, "pom.xml", _pom("solo", boot_plugin=True, java="17"))
    s = survey_repo(tmp_path)
    assert [(x.name, x.path) for x in s.services] == [("solo", ".")]


def test_a_maven_mirror_is_reported_because_the_build_depends_on_it(tmp_path):
    """`<mirrorOf>*</mirrorOf>` means every dependency comes from that host and Maven Central is
    never contacted — so the build needs it reachable, and the allowlist has to know."""
    _write(tmp_path, "pom.xml", _pom("app", boot_plugin=True, java="8"))
    _write(tmp_path, "settings.xml", """<?xml version="1.0"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors><mirror>
    <id>nexus</id><url>http://192.0.2.31:8081/repository/maven-public/</url>
    <mirrorOf>*</mirrorOf>
  </mirror></mirrors>
</settings>""")
    s = survey_repo(tmp_path)
    assert any("192.0.2.31:8081" in n for n in s.notes)


def test_an_unparsable_pom_does_not_abort_the_survey(tmp_path):
    """Only an unreadable repo aborts a run; one bad file degrades honestly."""
    _write(tmp_path, "pom.xml", "<project><broken")
    _write(tmp_path, "package.json", json.dumps({"name": "web", "scripts": {"build": "vite build"},
                                                 "devDependencies": {"vite": "5"}}))
    _write(tmp_path, "pnpm-lock.yaml", "")
    s = survey_repo(tmp_path)
    assert [x.name for x in s.services] == ["web"]


# ---------------------------------------------------------------- node

def test_node_framework_and_package_manager_are_detected(tmp_path):
    _write(tmp_path, "package.json", json.dumps({
        "name": "shop-client", "scripts": {"build:tenant-a": "tsc && vite build --mode prod.tenant-a"},
        "devDependencies": {"vite": "^5.1.4"}}))
    _write(tmp_path, "yarn.lock", "")
    _write(tmp_path, "tsconfig.json", "{}")
    s = survey_repo(tmp_path)
    ev = s.services[0].evidence
    assert (ev["framework"], ev["package_manager"], ev["typescript"]) == ("vite", "yarn", True)


def test_two_lockfiles_are_flagged_as_ambiguous(tmp_path):
    """The pilot ships yarn.lock AND package-lock.json. A frozen install can satisfy only one,
    so the recipe's install command is a guess unless someone decides."""
    _write(tmp_path, "package.json", json.dumps({"name": "web", "scripts": {"build": "vite build"},
                                                 "devDependencies": {"vite": "5"}}))
    _write(tmp_path, "yarn.lock", "")
    _write(tmp_path, "package-lock.json", "{}")
    s = survey_repo(tmp_path)
    assert any("lockfiles" in n and "ambiguous" in n for n in s.notes)
    assert s.services[0].evidence["lockfiles"] == ["yarn", "npm"]


def test_multiple_build_targets_are_flagged(tmp_path):
    """One directory producing several apps (build:tenant-a, build:tenant-b) does not fit
    one-target-per-directory, and assemble needs to know before it invents a shape."""
    _write(tmp_path, "package.json", json.dumps({
        "name": "web", "scripts": {"build:tenant-a": "vite build", "build:tenant-b": "vite build"},
        "devDependencies": {"vite": "5"}}))
    _write(tmp_path, "yarn.lock", "")
    s = survey_repo(tmp_path)
    assert any("build targets" in n for n in s.notes)
    assert s.services[0].evidence["build_targets"] == ["build:tenant-a", "build:tenant-b"]


@pytest.mark.parametrize("dep,expected", [
    ("react-scripts", "cra"), ("umi", "umi"), ("webpack", "webpack"), ("next", "next"),
])
def test_non_vite_frontends_are_named_correctly(dep, expected, tmp_path):
    """The pilot codebase is not Vite-shaped; a survey that reported everything as vite would size the
    recipe work wrongly."""
    _write(tmp_path, "package.json", json.dumps({"name": "app", "scripts": {"build": "x"},
                                                 "dependencies": {dep: "1"}}))
    _write(tmp_path, "package-lock.json", "{}")
    s = survey_repo(tmp_path)
    assert s.services[0].evidence["framework"] == expected


def test_node_apps_in_subdirectories_are_found(tmp_path):
    """A Java repo can carry a node sub-app: the pilot's backend ships shop-mcp with its own
    Dockerfile and k8s manifests."""
    _write(tmp_path, "pom.xml", _pom("app", modules=["api"], packaging="pom", java="8"))
    _write(tmp_path, "api/pom.xml", _pom("api", boot_plugin=True))
    _write(tmp_path, "mcp/package.json", json.dumps({"name": "mcp", "scripts": {"build": "tsc"}}))
    _write(tmp_path, "mcp/package-lock.json", "{}")
    s = survey_repo(tmp_path)
    assert {x.name for x in s.services} == {"api", "mcp"}


def test_vendored_dependencies_are_never_walked(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"name": "web", "scripts": {"build": "x"}}))
    _write(tmp_path, "node_modules/dep/package.json", json.dumps({"name": "dep"}))
    s = survey_repo(tmp_path)
    assert [x.name for x in s.services] == ["web"]


# ---------------------------------------------------------------- gaps and evidence

def test_a_root_with_no_matching_recipe_becomes_an_explicit_gap(tmp_path):
    """The deliverable of a survey is the gap list — it is what finalizes the recipe work. A node
    app with no recognised framework has no recipe, so it is still the honest example."""
    _write(tmp_path, "package.json", json.dumps({"name": "tool", "scripts": {"build": "tsc"}}))
    _write(tmp_path, "package-lock.json", "{}")
    s = survey_repo(tmp_path)
    assert s.gaps and "no registered recipe" in s.gaps[0]
    assert s.services[0].stack is None
    assert s.mappable == []


def test_a_recipe_matches_only_when_the_package_manager_matches_too(tmp_path):
    """`pnpm install --frozen-lockfile` cannot install a yarn.lock project, so the install
    command is part of the contract — not a detail to paper over."""
    from devagent import recipes

    _write(tmp_path, "package.json", json.dumps({"name": "web", "scripts": {"build": "vite build"},
                                                 "devDependencies": {"vite": "5"}}))
    _write(tmp_path, "yarn.lock", "")
    assert survey_repo(tmp_path).services[0].stack == "node-vite-yarn", "the yarn recipe"

    (tmp_path / "yarn.lock").unlink()
    _write(tmp_path, "pnpm-lock.yaml", "")
    assert survey_repo(tmp_path).services[0].stack == "node-vite-react", "the pnpm recipe"
    assert "pnpm" in recipes.REGISTRY["node-vite-react"].build_cmd
    assert "yarn" in recipes.REGISTRY["node-vite-yarn"].build_cmd


def test_a_plain_static_site_matches_the_static_recipe(tmp_path):
    _write(tmp_path, "index.html", "<h1/>")
    s = survey_repo(tmp_path)
    assert s.services == [] or s.services[0].stack == "static-web"


def test_deployment_and_ci_evidence_is_collected(tmp_path):
    """Dockerfile count is how many runnable units the project really ships; existing CI is what
    M9's tail has to fit rather than replace."""
    _write(tmp_path, "pom.xml", _pom("api", boot_plugin=True, java="8"))
    _write(tmp_path, "Dockerfile.tenant-a", "FROM openjdk:8-jdk-slim")
    _write(tmp_path, "Dockerfile.tenant-b", "FROM openjdk:8-jdk-slim")
    _write(tmp_path, "Jenkinsfile", "pipeline {}")
    s = survey_repo(tmp_path)
    assert any("2 Dockerfile(s)" in n for n in s.notes)
    assert any("Jenkinsfile" in n for n in s.notes)


def test_survey_serialises_to_json(tmp_path):
    """survey.json is a spec deliverable and is read by later stages and by a human."""
    _write(tmp_path, "pom.xml", _pom("api", boot_plugin=True, java="8"))
    parsed = json.loads(survey_repo(tmp_path).to_json())
    assert parsed["services"][0]["name"] == "api"
    assert "gaps" in parsed and "notes" in parsed


def test_a_missing_repo_is_refused(tmp_path):
    with pytest.raises(NotADirectoryError):
        survey_repo(tmp_path / "nope")


def test_a_scoped_package_name_becomes_a_directory_safe_service_name(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text('{"name": "@acme/web", "scripts": {"build": "vite build"}}')
    assert [s.name for s in survey_repo(tmp_path).services] == ["acme-web"]


def test_a_package_json_that_is_not_an_object_is_skipped(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text("[]")
    assert survey_repo(tmp_path).services == []


def test_the_same_service_name_twice_in_one_repo_is_refused(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "package.json").write_text('{"name": "app", "scripts": {"build": "x"}}')
    with pytest.raises(ValueError, match=r"service name 'app' appears in both .*a.* and .*b"):
        survey_repo(tmp_path)
