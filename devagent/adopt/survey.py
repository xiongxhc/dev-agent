"""Survey — find the service roots in an existing repo, deterministically and without an LLM.

This is the stage that decides what the rest of adopt even looks at, so it is
pure Python and fully testable against fixture trees: a mapping session is expensive and a
model asked "what services are here" will invent a plausible answer, whereas a `pom.xml`'s
`<modules>` list is a fact.

Three things it must get right:

* **Declared modules, not directories.** A multi-module Maven repo can have directories that
  are not part of the build. Walking directories adopts things the project does not build.
* **Deployable versus library.** Only some modules are services. A deployable module carries
  `spring-boot-maven-plugin` or a `*Application.java`. Mapping a library as a service
  produces a design with no runnable shape.
* **Explicit gaps.** A root with no matching recipe is reported as a gap, not dropped. The
  gap list is the evidence for which recipes are still missing — silence would hide exactly
  that work.

It also collects what a build would need but cannot infer later: the Java version, the package
manager (by lockfile), and any Maven mirror, because a mirror with `<mirrorOf>*</mirrorOf>`
means the build never reaches Maven Central and the mirror's host has to be reachable instead.
"""

import ipaddress
import json
import os
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

#: Never source, at any depth: version control, vendored dependencies, interpreter caches.
_NEVER_SOURCE = frozenset({".git", "node_modules", ".venv", "__pycache__"})
#: Build output, named by the build file that sits beside it. The same name elsewhere (a Java
#: package called `build`, a committed `static/dist/`) is source.
_BUILD_OUTPUT = {"pom.xml": frozenset({"target"}),
                 "package.json": frozenset({"dist", "build", "out"})}


def not_source(parent: Path, name: str) -> bool:
    """Is the entry `name` inside directory `parent` something neither the survey nor the source
    copy should treat as source?"""
    if name in _NEVER_SOURCE:
        return True
    return any(name in outputs and (parent / build_file).is_file()
               for build_file, outputs in _BUILD_OUTPUT.items())

_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


def slug(name: str) -> str:
    """The design id for a service or library name. Ids are what must be unique in a system."""
    return name.replace("_", "-").lower()


@dataclass
class ServiceRoot:
    """One thing in the repo that might be a service."""
    name: str
    path: str                     # relative to the repo root; "." for the root itself
    kind: str                     # maven-module | node-app | static-site
    deployable: bool              # a runnable service, as opposed to a library
    stack: str | None = None      # matched recipe name
    gap: str | None = None        # why nothing matched, when nothing did
    evidence: dict = field(default_factory=dict)
    repo: str = ""                # the repo this root came from; set when surveys are merged


#: What a service name must look like before it becomes a directory, a container name or argv.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _safe_name(text) -> str:
    """`text` reduced to `[A-Za-z0-9][A-Za-z0-9._-]*`, or "" when nothing usable is left. A name
    becomes a directory, a container name and half of a `docker -v host:container` mount, so a
    `:`, a space or a leading `-` would change what those mean. Each disallowed character
    becomes `-`; leading characters that are not alphanumeric (which also rules out `.`, `..`
    and all-dot names) are dropped."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"^[^A-Za-z0-9]+", "", re.sub(r"[^A-Za-z0-9._-]", "-", text))


def _root(name, fallback: str, **fields) -> ServiceRoot:
    """The one place a ServiceRoot is built, so no name can skip the naming rule: the name, else
    the fallback (a directory name), else `service`, each reduced by `_safe_name`."""
    return ServiceRoot(name=_safe_name(name) or _safe_name(fallback) or "service", **fields)


@dataclass
class Survey:
    repo: str
    services: list = field(default_factory=list)
    libraries: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    notes: list = field(default_factory=list)   # facts a later stage needs (mirrors, modes)
    mirror_hosts: list = field(default_factory=list)   # package mirrors the build must reach

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------- maven

def _pom(path: Path):
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def _local(tag: str) -> str:
    """The tag without its namespace. Maven files use several (the POM namespace, the SETTINGS
    namespace, sometimes none), so matching a fixed one silently finds nothing and a
    settings.xml mirror goes unreported."""
    return tag.rpartition("}")[2]


def _text(node, tag: str) -> str | None:
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == tag and child.text:
            return child.text.strip()
    return None


def _findall(node, tag: str):
    return [e for e in node.iter() if _local(e.tag) == tag]


def _children(node, tag: str) -> list:
    return [] if node is None else [e for e in node if _local(e.tag) == tag]


def _module_list(node) -> list[str]:
    """The `<modules>/<module>` entries directly under `node`, stripped, blank and repeated
    entries dropped. Reading only direct children keeps a profile's modules, which are not in
    the default build, out of the list."""
    out = []
    for modules in _children(node, "modules"):
        for m in _children(modules, "module"):
            text = (m.text or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def _java_version(root) -> str | None:
    """`<java.version>` or the compiler source declared in this POM's properties."""
    if root is None:
        return None
    props = root.find(f"{_POM_NS}properties")
    if props is None:
        props = root.find("properties")
    for tag in ("java.version", "maven.compiler.source", "maven.compiler.release"):
        value = _text(props, tag)
        if value:
            return value
    return None


def java_major(value: str | None) -> int | None:
    """The major Java version of a declared value: `1.8` and `8` are 8, `17` is 17. A property
    reference such as `${jdk}` or anything else that is not a number is unknown."""
    m = re.fullmatch(r"(?:1\.)?(\d+)(?:\.\d+)*", (value or "").strip())
    return int(m.group(1)) if m else None


def _maven_mirrors(repo: Path) -> list[str]:
    """Mirror URLs from a settings.xml beside the pom.

    A `<mirrorOf>*</mirrorOf>` mirror means every dependency comes from there and Maven Central
    is never contacted — so the build needs that host reachable, and only that host."""
    settings = repo / "settings.xml"
    root = _pom(settings) if settings.is_file() else None
    if root is None:
        return []
    urls = []
    for mirror in _findall(root, "mirror"):
        url = _text(mirror, "url")
        if url and url not in urls:
            urls.append(url)
    return urls


def _is_boot_app(module_dir: Path, root) -> tuple[bool, dict]:
    """Deployable if it builds an executable artifact or has a Spring entry point."""
    evidence = {}
    plugins = [_text(p, "artifactId") for p in _findall(root, "plugin")] if root is not None else []
    if "spring-boot-maven-plugin" in plugins:
        evidence["spring_boot_maven_plugin"] = True
    src = module_dir / "src" / "main" / "java"
    apps, bare_mains = [], []
    if src.is_dir():
        for f in src.rglob("*.java"):
            try:
                body = f.read_text(errors="ignore")
            except OSError:
                continue
            if "@SpringBootApplication" in body:
                apps.append(f.name)
            elif "public static void main(" in body:
                bare_mains.append(f.name)
            if len(apps) >= 3:
                break
    if apps:
        evidence["entry_points"] = sorted(apps)
    if bare_mains:
        # Deliberately NOT decisive. A library can carry a demo main(); treating any main() as
        # a service turns every such library into a service and the design into fiction.
        evidence["bare_mains"] = sorted(bare_mains)[:3]
    return bool(apps) or evidence.get("spring_boot_maven_plugin", False), evidence


def _maven_reactors(repo: Path) -> list[Path]:
    """Where this repo's Maven builds actually start.

    The root when it carries a pom.xml; otherwise its immediate children that do. Reading only
    `<repo>/pom.xml` is a silent blind spot for a repo that keeps a Maven `backend/` beside a
    node `frontend/`: the node walk finds the frontend, the backend is missed, and no gap is
    reported, so the adoption looks complete while describing half the system. One level, the
    same depth as the node walk; deeper would be guessing at directory habits."""
    if (repo / "pom.xml").is_file():
        return [repo]
    return [d for d in sorted(repo.iterdir())
            if d.is_dir() and not not_source(repo, d.name) and (d / "pom.xml").is_file()]


def _survey_maven(repo: Path, survey: Survey, label: str = "") -> None:
    """Survey ONE reactor. `repo` is the reactor root — the directory the build runs from — and
    every root it emits records that as its `repo`, because that is what a module build and the
    source copy both key on (`mvn -pl <module> -am` from the reactor, not from above it)."""
    root_pom = repo / "pom.xml"
    root = _pom(root_pom)
    if root is None:
        return
    java = _java_version(root)
    mirrors = _maven_mirrors(repo)
    if mirrors:
        for url in mirrors:
            _mirror_url(survey, url, f"{label}settings.xml")
        survey.notes.append(
            f"{label}maven mirrors {mirrors} — settings.xml redirects dependency resolution, so "
            f"the build needs those hosts reachable (and may never contact Maven Central)")

    declared = _module_list(root)
    _note_profile_modules(root, declared, label, survey)
    packaging = _text(root, "packaging")
    if not declared:
        # Single-module project: the reactor root IS the service.
        deployable, evidence = _is_boot_app(repo, root)
        survey.services.append(_root(
            _text(root, "artifactId"), repo.name, path=".", kind="maven-module",
            deployable=deployable, evidence={"java_version": java, **evidence},
            repo=str(repo)))
        return

    survey.notes.append(f"{label}maven parent packaging={packaging}, {len(declared)} declared "
                        f"module(s) — directories not in <modules> are not part of the build")
    _survey_modules(repo, repo, "", declared, java, survey, {os.path.realpath(repo)}, label)

    undeclared = [d.name for d in repo.iterdir()
                  if d.is_dir() and not not_source(repo, d.name) and (d / "pom.xml").is_file()
                  and d.name not in declared]
    if undeclared:
        survey.notes.append(f"{label}maven directories present but NOT declared modules: "
                            f"{sorted(undeclared)} — excluded, they are not built by this repo")


def _note_profile_modules(pom_root, declared: list, label: str, survey: Survey) -> None:
    """One note per profile that adds modules to those `declared` by default. They are built
    only when the profile is active, so they are not surveyed."""
    for profile in (p for ps in _children(pom_root, "profiles")
                    for p in _children(ps, "profile")):
        extra = [m for m in _module_list(profile) if m not in declared]
        if extra:
            survey.notes.append(
                f"{label}maven profile {_text(profile, 'id') or '(no id)'!r} adds module(s) "
                f"{extra} — not in the default build, so not surveyed")


def _survey_modules(reactor: Path, base: Path, prefix: str, declared: list, java,
                    survey: Survey, walking: set, label: str) -> None:
    """Survey the modules an aggregator at `base` declares. A module whose own POM declares
    modules is an aggregator too and is walked into: its modules are what the build produces.
    Every root keeps `reactor` as its `repo` and a path relative to it, so a module build still
    runs `-pl <path> -am` from the top. `walking` holds the aggregators on the current path,
    so a module that names one of them is surveyed as a module rather than walked again."""
    for module in declared:
        module_dir = base / module
        path = posixpath.normpath(posixpath.join(prefix, module.replace("\\", "/"))) \
            if prefix else module
        module_root = _pom(module_dir / "pom.xml")
        # A module's own declaration outranks the parent's: it selects the recipe.
        # (When the module's value does not resolve, the parent's is used; the raw text is kept
        # only if neither resolves, so the gap can quote it.)
        own = _java_version(module_root)
        module_java = own if java_major(own) is not None else (java or own)
        nested = _module_list(module_root)
        real = os.path.realpath(module_dir)
        if nested and real not in walking:
            _note_profile_modules(module_root, nested, f"{label}{path}: ", survey)
            _survey_modules(reactor, module_dir, path, nested, module_java, survey,
                            walking | {real}, label)
            continue
        deployable, evidence = _is_boot_app(module_dir, module_root)
        evidence["java_version"] = module_java
        last = os.path.basename(os.path.normpath(module.replace("\\", "/")))
        entry = _root(last, Path(os.path.normpath(module_dir)).name, path=path,
                      kind="maven-module", deployable=deployable, evidence=evidence,
                      repo=str(reactor))
        (survey.services if deployable else survey.libraries).append(entry)


# ---------------------------------------------------------------- node

_PM_LOCKS = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"),
             ("bun.lockb", "bun"))


def _has_build_script(pkg: dict) -> bool:
    return any(k == "build" or k.startswith("build:") for k in (pkg.get("scripts") or {}))


def _package_manager(d: Path) -> tuple[str | None, list[str]]:
    found = [(pm, lock) for lock, pm in _PM_LOCKS if (d / lock).is_file()]
    if not found:
        return None, []
    return found[0][0], [pm for pm, _ in found]


def _node_framework(pkg: dict) -> str | None:
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    for name, label in (("next", "next"), ("vite", "vite"), ("react-scripts", "cra"),
                        ("@umijs/max", "umi"), ("umi", "umi"), ("webpack", "webpack")):
        if name in deps:
            return label
    return None


def _package_service_name(package_name) -> str | None:
    """The package name made directory-safe: a scope's leading `@` is dropped and `/` becomes
    `-`, so `@acme/web` is `acme-web`."""
    if isinstance(package_name, str) and package_name:
        return package_name.removeprefix("@").replace("/", "-")
    return None


def _survey_node(d: Path, repo: Path, survey: Survey) -> dict | None:
    """Survey the package at `d`; returns the parsed package.json, or None when there is none
    usable. Only a package with a `build` or `build:*` script is a service: a package that
    builds nothing (commit hooks, formatters, a workspace root) has no artifact to prove."""
    try:
        pkg = json.loads((d / "package.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(pkg, dict):
        return None
    scripts = pkg.get("scripts") or {}
    build_scripts = sorted(k for k in scripts if k == "build" or k.startswith("build:"))
    rel = "." if d == repo else str(d.relative_to(repo))
    deployable = bool(build_scripts)
    if not deployable:
        survey.notes.append(f"{rel}: package {pkg.get('name') or d.name!r} has no build or "
                            f"build:* script — it builds nothing, so it is not a service")
        return pkg
    pm, all_pms = _package_manager(d)
    framework = _node_framework(pkg)

    evidence = {"package_manager": pm, "framework": framework,
                "build_scripts": build_scripts,
                "typescript": (d / "tsconfig.json").is_file()}
    if len(all_pms) > 1:
        survey.notes.append(f"{rel}: {len(all_pms)} lockfiles present ({all_pms}) — the package "
                            f"manager is ambiguous and a frozen install can only satisfy one; "
                            f"{pm} assumed by precedence")
        evidence["lockfiles"] = all_pms
    modes = [s for s in build_scripts if s.startswith("build:")]
    if len(modes) > 1:
        survey.notes.append(f"{rel}: {len(modes)} build targets ({modes}) — one directory "
                            f"produces several apps, which does not fit one-target-per-directory")
        evidence["build_targets"] = modes
    npmrc = d / ".npmrc"
    if npmrc.is_file():
        for line in npmrc.read_text(errors="ignore").splitlines():
            m = re.match(r"\s*(?:@[\w-]+:)?registry\s*=\s*(\S+)", line)
            if m:
                known = set(survey.mirror_hosts)
                host = _mirror_url(survey, m.group(1), f"{rel}: .npmrc")
                if host and host not in known:
                    survey.notes.append(
                        f"{rel}: .npmrc points at {host} — the install needs that host, not the "
                        f"public registry")
    out_dir = re.search(r"outDir:\s*([^\n,]+)", (d / "vite.config.ts").read_text(errors="ignore")) \
        if (d / "vite.config.ts").is_file() else None
    if out_dir:
        evidence["out_dir"] = out_dir.group(1).strip()

    survey.services.append(_root(
        _package_service_name(pkg.get("name")), d.name, path=rel, kind="node-app",
        deployable=deployable, evidence=evidence))
    return pkg


# ---------------------------------------------------------------- static site

def _survey_static_site(repo: Path, survey: Survey) -> None:
    """A repo with no Maven or node service whose root holds an `index.html` is one static site.
    Anything already found wins: an `index.html` beside a `package.json` is that app's entry
    page, not a second service."""
    if survey.services or survey.libraries or not (repo / "index.html").is_file():
        return
    survey.services.append(_root(repo.name, "", path=".", kind="static-site", deployable=True,
                                 evidence={"entry": "index.html"}, repo=str(repo)))
    survey.notes.append("static site detected at the repo root (index.html, no build manifest)")


# ---------------------------------------------------------------- deployment evidence

def _deployment_evidence(repo: Path, survey: Survey) -> None:
    dockerfiles = sorted(p.name for p in repo.glob("Dockerfile*"))
    composes = sorted(p.name for p in repo.glob("docker-compose*"))
    if dockerfiles:
        survey.notes.append(f"{len(dockerfiles)} Dockerfile(s): {dockerfiles} — deployment "
                            f"evidence for how many runnable units the project really ships")
    if composes:
        survey.notes.append(f"compose file(s): {composes}")
    for ci in ("Jenkinsfile", ".gitlab-ci.yml", ".github/workflows"):
        if (repo / ci).exists():
            survey.notes.append(f"existing CI: {ci} — later stages fit it, not replace it")


# ---------------------------------------------------------------- recipe matching

def match_recipes(survey: Survey, registry=None) -> None:
    """Fill in `stack` where a registered recipe fits, and `gap` where none does.

    A gap is a first-class output: it names the recipe that is still missing, so "needs a Maven
    recipe for Java 9" is the deliverable, not a failure."""
    if registry is None:
        from .. import recipes as _recipes
        registry = _recipes.REGISTRY
    by_type: dict = {}
    for name, recipe in registry.items():
        by_type.setdefault(recipe.type, []).append((name, recipe))

    for svc in survey.services:
        ev = svc.evidence
        if svc.kind == "maven-module":
            declared = ev.get("java_version")
            major = java_major(declared)
            svc.stack = _match_maven(by_type.get("backend", []), major)
            if major is not None:
                want, tail = f"a Maven recipe for Java {major}", " — no registered recipe"
            elif declared:
                want = "a Maven recipe, but its Java version is declared as "
                want += f"{declared!r}, which this survey does not resolve"
                tail = ""
            else:
                want = "a Maven recipe for an undeclared Java version"
                tail = " — declare java.version in the POM"
        elif svc.kind == "static-site":
            svc.stack = "static-web" if "static-web" in registry else None
            want, tail = "static-web", " — no registered recipe"
        else:
            fw = ev.get("framework") or "unknown"
            pm = ev.get("package_manager") or "unknown"
            # An unknown package manager means there is NO lockfile, which is a different
            # problem from a missing recipe: no install can be frozen, so two builds of the same
            # commit may resolve different trees and a `proven` verdict would mean nothing. Name
            # it, or the gap reads as recipe work when it is repo work.
            tail = " — no registered recipe"
            want = f"node {fw} + {'no lockfile' if pm == 'unknown' else pm}"
            svc.stack = _match_node(by_type.get("frontend", []), fw, pm)
        if not svc.stack:
            svc.gap = f"{svc.name}: needs {want}{tail}"
            survey.gaps.append(svc.gap)


def _match_maven(candidates, major: int | None) -> str | None:
    """A Maven recipe fits a module whose Java version it builds. Recipes are per Java version
    (`...-jdk17`); one with no version suffix builds any, and is the fallback."""
    maven = [n for n, _ in candidates if "maven" in n]
    if major is not None:
        for name in maven:
            if name.endswith(f"-jdk{major}"):
                return name
    return next((n for n in maven if not re.search(r"-jdk\d+$", n)), None)


def _match_node(candidates, framework: str, pm: str) -> str | None:
    """A recipe fits only if BOTH the framework and the package manager match: the install
    command is part of the recipe's build contract, and `pnpm --frozen-lockfile` cannot install
    a yarn.lock project."""
    for name, recipe in candidates:
        install = (recipe.build_cmd or "").split()
        recipe_pm = next((p for p in ("pnpm", "yarn", "npm") if p in install), None)
        if framework and framework in name and recipe_pm == pm:
            return name
        if not recipe_pm and framework and framework in name:
            return name
    # No fallback to the static-site recipe: it copies files instead of building them, so on a
    # node app it would report a build that never ran. A static site is its own `kind`.
    return None


# ---------------------------------------------------------------- entry point

#: Second-level labels under which a country code is a public suffix (`co.uk`, `com.au`).
_PUBLIC_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac"})


def _mirror_url(survey, url: str, where: str) -> str | None:
    """Record the host a mirror URL names and return it, or None when nothing was recorded.

    The egress proxy matches the CONNECT host without its port, so only the hostname is kept.
    The proxy tunnels HTTPS only: a plain `http://` mirror cannot be reached through it, so it
    is reported instead of allowlisted, rather than telling the operator it is reachable."""
    parts = urlsplit(url.strip())
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        return None
    if parts.scheme.lower() == "http":
        survey.notes.append(
            f"{where}: mirror {host} is plain http — the egress proxy tunnels HTTPS only, so "
            f"with egress on the build cannot reach it; it is not allowlisted")
        return None
    _record_mirror(survey, host)
    return host


def _sibling_suffix(host: str) -> str | None:
    """`host` with its first label dropped, when that cannot be a public suffix. A country-code
    TLD (two letters) may sit under a public second level (`example.co.uk`), so a host ending
    in one gets no suffix, nor does one whose second-last label is a common public second
    level. An IP address never gets one: ".2.31" would match any address ending that way."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 3 or len(labels[-1]) <= 2 or labels[-2] in _PUBLIC_SECOND_LEVEL:
        return None
    return "." + ".".join(labels[1:])


def _record_mirror(survey, host: str) -> None:
    """Record a package mirror, plus its sibling-host suffix.

    A registry commonly redirects tarball downloads to a sibling host (a mirror may serve metadata
    from registry.* and content from cdn.*). The redirect happens INSIDE the TLS tunnel, so the
    client opens a second CONNECT the allowlist has never heard of and the build fails naming the
    original URL. The built-in defaults already treat npmjs this way (`.npmjs.org`)."""
    for candidate in (host, _sibling_suffix(host)):
        if candidate and candidate not in survey.mirror_hosts:
            survey.mirror_hosts.append(candidate)


def _collision(first_name: str, first_at: str, name: str, at: str) -> ValueError:
    """The error for two services that would get the same design id (the slug of the name)."""
    fix = ("service ids must be unique across a system, so one of them needs renaming before "
           "it can be adopted")
    if first_name == name:
        return ValueError(f"service name {name!r} appears in both {first_at} and {at} — {fix}")
    return ValueError(f"service names {first_name!r} (at {first_at}) and {name!r} (at {at}) "
                      f"both give the id {slug(name)!r} — {fix}")


def _refuse_duplicate_names(survey: Survey, repo: Path) -> None:
    """Refuse two services whose names give the same id; `survey_repos` applies the same rule
    across repos. Libraries are not checked.

    Paths are shown relative to the repo and built only on a collision: a module may live
    outside the repo tree (`../shared`), which a subpath computation would reject."""
    seen: dict = {}
    for svc in survey.services:
        sid = slug(svc.name)
        if sid not in seen:
            seen[sid] = svc
            continue
        first = seen[sid]
        raise _collision(first.name, _shown(first, repo), svc.name, _shown(svc, repo))


def _shown(svc: ServiceRoot, repo: Path) -> str:
    return os.path.relpath(os.path.join(svc.repo, svc.path), repo)


def survey_repo(repo_path, registry=None) -> Survey:
    """Walk a repo and report its service roots, libraries, gaps and build-relevant facts."""
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise NotADirectoryError(f"{repo} is not a directory")
    survey = Survey(repo=str(repo))

    reactors = _maven_reactors(repo)
    for reactor in reactors:
        _survey_maven(reactor, survey,
                      label="" if reactor == repo else f"{reactor.relative_to(repo)}: ")

    # A node app can sit one level below either build root, the repo or a sub-reactor. Both are
    # scanned so a node service is found the same way whichever directory was adopted.
    # A package at the base claims the directories below it only when it is itself the app
    # (it has a build script) or a workspace root; a tooling package does not.
    for base in [repo] + [r for r in reactors if r != repo]:
        if (base / "package.json").is_file():
            pkg = _survey_node(base, repo, survey)
            if pkg is not None and (_has_build_script(pkg) or "workspaces" in pkg):
                continue
        for d in sorted(p for p in base.iterdir()
                        if p.is_dir() and not not_source(base, p.name)):
            if (d / "package.json").is_file():
                _survey_node(d, repo, survey)
    _survey_static_site(repo, survey)
    _deployment_evidence(repo, survey)
    match_recipes(survey, registry)
    for root in survey.services + survey.libraries:
        # A sub-reactor already recorded its own root; overwriting it here would send the build
        # and the source copy one directory too high, where there is no pom.xml.
        root.repo = root.repo or str(repo)
    _refuse_duplicate_names(survey, repo)
    return survey


def survey_repos(paths, registry=None) -> Survey:
    """Survey several repos as ONE system.

    A system is not always one repository. A Spring Boot backend and a React frontend can live
    in two repositories, and adopting either alone produces a design whose frontend
    consumes an API no service in it provides — which the wiring gate rightly refuses. Merging
    the surveys is what makes the gate meaningful rather than an obstacle."""
    paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
    merged = Survey(repo=", ".join(str(Path(p).resolve()) for p in paths))
    seen = {}
    for path in paths:
        one = survey_repo(path, registry)
        for root in one.services:
            sid = slug(root.name)
            if sid in seen:
                first_name, first_repo = seen[sid]
                raise _collision(first_name, first_repo, root.name, root.repo)
            seen[sid] = (root.name, root.repo)
            merged.services.append(root)
        merged.libraries.extend(one.libraries)
        merged.gaps.extend(one.gaps)
        merged.mirror_hosts.extend(h for h in one.mirror_hosts if h not in merged.mirror_hosts)
        merged.notes.extend(f"[{Path(root_path).name}] {n}"
                            for root_path, n in ((path, n) for n in one.notes))
    return merged
