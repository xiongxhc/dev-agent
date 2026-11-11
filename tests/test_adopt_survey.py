"""Survey, on fixture trees.

Survey decides what the rest of adopt looks at, so a wrong answer is not a small error: a library
adopted as a service produces a design with no runnable shape, and a missed recipe gap hides the
work still to be done.
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
    """A repo ships directories that are not part of its build — a sibling tool directory is not
    a module. Walking directories adopts what the project does not build."""
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
    """A library can carry a demo `public static void main`. Treating any main() as a service
    turns libraries into services."""
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
    """The Java version selects the recipe, and it is usually stated only in the parent."""
    _write(tmp_path, "pom.xml", _pom("app", modules=["api"], packaging="pom", java="8"))
    _write(tmp_path, "api/pom.xml", _pom("api", boot_plugin=True))
    s = survey_repo(tmp_path)
    assert s.services[0].evidence["java_version"] == "8"
    assert s.services[0].stack == "maven-spring-boot-jdk8", "a matched recipe for Java 8"


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
    assert s.mirror_hosts == ["192.0.2.31"], "an IP must not add a domain suffix"


def test_a_hostname_mirror_also_records_its_registrable_domain(tmp_path):
    """A registry may redirect downloads to a sibling host, so the two-label suffix joins the
    allowlist for a three-label hostname."""
    _write(tmp_path, "pom.xml", _pom("app", boot_plugin=True, java="8"))
    _write(tmp_path, "settings.xml", """<?xml version="1.0"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors><mirror>
    <id>m</id><url>https://registry.example.com/maven/</url><mirrorOf>*</mirrorOf>
  </mirror></mirrors>
</settings>""")
    assert survey_repo(tmp_path).mirror_hosts == ["registry.example.com", ".example.com"]


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
        "name": "client", "scripts": {"build:prod": "tsc && vite build --mode prod"},
        "devDependencies": {"vite": "^5.1.4"}}))
    _write(tmp_path, "yarn.lock", "")
    _write(tmp_path, "tsconfig.json", "{}")
    s = survey_repo(tmp_path)
    ev = s.services[0].evidence
    assert (ev["framework"], ev["package_manager"], ev["typescript"]) == ("vite", "yarn", True)


def test_two_lockfiles_are_flagged_as_ambiguous(tmp_path):
    """A project can ship yarn.lock AND package-lock.json. A frozen install satisfies only one,
    so the recipe's install command is a guess unless someone decides."""
    _write(tmp_path, "package.json", json.dumps({"name": "web", "scripts": {"build": "vite build"},
                                                 "devDependencies": {"vite": "5"}}))
    _write(tmp_path, "yarn.lock", "")
    _write(tmp_path, "package-lock.json", "{}")
    s = survey_repo(tmp_path)
    assert any("lockfiles" in n and "ambiguous" in n for n in s.notes)
    assert s.services[0].evidence["lockfiles"] == ["yarn", "npm"]


def test_multiple_build_targets_are_flagged(tmp_path):
    """One directory producing several apps (build:staging, build:prod) does not fit
    one-target-per-directory, and assemble needs to know before it invents a shape."""
    _write(tmp_path, "package.json", json.dumps({
        "name": "web", "scripts": {"build:staging": "vite build", "build:prod": "vite build"},
        "devDependencies": {"vite": "5"}}))
    _write(tmp_path, "yarn.lock", "")
    s = survey_repo(tmp_path)
    assert any("build targets" in n for n in s.notes)
    assert s.services[0].evidence["build_targets"] == ["build:prod", "build:staging"]


@pytest.mark.parametrize("dep,expected", [
    ("react-scripts", "cra"), ("umi", "umi"), ("webpack", "webpack"), ("next", "next"),
])
def test_non_vite_frontends_are_named_correctly(dep, expected, tmp_path):
    """Frontends are not all Vite; a survey that reported everything as vite would size the
    recipe work wrongly."""
    _write(tmp_path, "package.json", json.dumps({"name": "app", "scripts": {"build": "x"},
                                                 "dependencies": {dep: "1"}}))
    _write(tmp_path, "package-lock.json", "{}")
    s = survey_repo(tmp_path)
    assert s.services[0].evidence["framework"] == expected


def test_node_apps_in_subdirectories_are_found(tmp_path):
    """A Java repo can carry a node sub-app beside its Maven modules."""
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
    later stages have to fit rather than replace."""
    _write(tmp_path, "pom.xml", _pom("api", boot_plugin=True, java="8"))
    _write(tmp_path, "Dockerfile.api", "FROM openjdk:8-jdk-slim")
    _write(tmp_path, "Dockerfile.worker", "FROM openjdk:8-jdk-slim")
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


def _boot_app(root, java_version):
    """A single-module Spring Boot repo declaring `java_version` (or none)."""
    _write(root, "pom.xml", _pom("app", boot_plugin=True, java=java_version))
    _write(root, "src/main/java/App.java", "@SpringBootApplication class App {}")


def _node_app(root, rel, name):
    _write(root, f"{rel}/package.json",
           json.dumps({"name": name, "scripts": {"build": "x"}}))


# ---------------------------------------------------------------- java version -> recipe

@pytest.mark.parametrize("declared,stack", [
    ("1.8", "maven-spring-boot-jdk8"), ("11", "maven-spring-boot-jdk11"),
    ("17", "maven-spring-boot-jdk17"), ("21", "maven-spring-boot-jdk21")])
def test_a_maven_service_gets_the_recipe_for_its_declared_java_version(tmp_path, declared, stack):
    _boot_app(tmp_path, java_version=declared)
    assert survey_repo(tmp_path).services[0].stack == stack


def test_an_unsupported_java_version_is_a_gap_that_names_it(tmp_path):
    _boot_app(tmp_path, java_version="9")
    s = survey_repo(tmp_path)
    assert s.services[0].stack is None and "Java 9" in s.gaps[0]


def test_an_undeclared_java_version_is_a_gap_not_a_guess(tmp_path):
    _boot_app(tmp_path, java_version=None)
    s = survey_repo(tmp_path)
    assert s.services[0].stack is None and "undeclared Java version" in s.gaps[0]


def test_an_external_maven_recipe_without_a_jdk_suffix_matches_any_version(tmp_path):
    from devagent.recipes.base import Recipe, Toolchain

    _boot_app(tmp_path, java_version="9")
    registry = {"maven-custom": Recipe(name="maven-custom", type="backend",
                                       toolchain=Toolchain(image="img"))}
    assert survey_repo(tmp_path, registry).services[0].stack == "maven-custom"


@pytest.mark.parametrize("value,major", [
    ("1.8", 8), ("8", 8), ("17", 17), ("21", 21), ("11.0.2", 11),
    (None, None), ("", None), ("${jdk.version}", None), ("latest", None)])
def test_java_major_reads_declared_values(value, major):
    from devagent.adopt.survey import _java_major

    assert _java_major(value) == major


# ---------------------------------------------------------------- names

def test_a_scoped_package_name_becomes_a_directory_safe_service_name(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text(
        '{"name": "@acme/web", "scripts": {"build": "vite build"}}')
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


def test_names_that_give_the_same_service_id_are_refused(tmp_path):
    """The design id is the slug of the name, so `my_app` and `my-app` collide."""
    _node_app(tmp_path, "a", "my_app")
    _node_app(tmp_path, "b", "my-app")
    with pytest.raises(ValueError, match="appears in both a and b"):
        survey_repo(tmp_path)


def test_a_maven_module_and_a_node_directory_sharing_a_name_are_refused(tmp_path):
    """The message shows repo-relative paths, not absolute ones."""
    _write(tmp_path, "pom.xml", _pom("root", modules=["app"], packaging="pom", java="17"))
    _write(tmp_path, "app/pom.xml", _pom("app", boot_plugin=True))
    _node_app(tmp_path, "web", "app")
    with pytest.raises(ValueError) as err:
        survey_repo(tmp_path)
    msg = str(err.value)
    assert "service name 'app' appears in both app and web " in msg
    assert str(tmp_path) not in msg


def test_a_maven_module_outside_the_repo_tree_is_not_an_error(tmp_path):
    """`<module>../shared</module>` is legal Maven; it must not break the survey."""
    repo = tmp_path / "repo"
    _write(repo, "pom.xml", _pom("root", modules=["../shared"], packaging="pom", java="17"))
    _write(tmp_path, "shared/pom.xml", _pom("shared", boot_plugin=True))
    _node_app(repo, "web", "web")
    assert len(survey_repo(repo).services) == 2
