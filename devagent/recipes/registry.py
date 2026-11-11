# devagent/recipes/registry.py
"""The recipe REGISTRY. M6 registers two Node recipes (no new image). Adding a recipe
makes a new artifact type buildable with NO harness change — that is the extensibility
seam M7 (Java/Python, brownfield detection) plugs into.

M11: recipes are also loadable as DATA. Point `DEVAGENT_RECIPES_DIR` at a directory of `*.json`
manifests (each a recipe dict, or a list of them) and they merge into the REGISTRY at import —
so a new stack/language is a config file, not a code edit. A manifest with a built-in's name
overrides it. (The manifest registers the recipe; a brand-new toolchain *image* it references
must still be built — the bundled `devagent-sandbox:m2` already carries node + python3.)"""

import json
import os
from pathlib import Path

from .base import BootSpec, Recipe, ServiceSpec, Toolchain, recipe_from_dict

NODE = Toolchain(image="devagent-sandbox:m2")    # node + pnpm + chromium + python3; npm allowlist

_FRONTEND_HINT = (
    "Scaffold a Vite + React + Tailwind app in this target's directory: package.json "
    "(PIN exact dependency versions — no ^ or ~), vite.config, tailwind.config, "
    "postcss.config, index.html, and src/. Implement every page (a route that renders) "
    "and satisfy every acceptance check. For the API base URL: at app startup fetch "
    "`/config.json` and use its `apiBase` field as the API base URL (the deploy step "
    "writes this file into dist/ so the pre-built bundle discovers the backend URL at "
    "runtime without a rebuild).\n"
    "DESIGN — make it look intentional, not templated. Commit to ONE visual direction that "
    "fits what this app actually IS: a specific palette (define 4-6 colors as Tailwind theme "
    "tokens), a deliberate type pairing (a characterful display face + a readable body face "
    "loaded as real web fonts, not the system default), and a consistent spacing/type scale. "
    "CONTRAST IS NON-NEGOTIABLE: every button, link, input, and icon MUST have explicit, "
    "high-contrast colors and a clearly visible hover AND keyboard-focus state — NEVER leave an "
    "interactive element at a browser default, transparent, or white-on-white (a control the "
    "user cannot see is a bug, not a style). Hit a quality floor without announcing it: "
    "responsive down to mobile, visible focus rings, `prefers-reduced-motion` respected, real "
    "semantic labels/aria. Write UI copy in plain active voice from the user's side — a control "
    "says exactly what it does (\"Sign in\", \"Add task\", \"Log out\"), and keeps that name "
    "through the flow. AVOID the generic AI-default looks (cream #F4F1EA + serif + terracotta; "
    "near-black + one acid-green/vermilion accent; broadsheet hairline columns); spend boldness "
    "on ONE signature element and keep everything around it quiet and disciplined."
)
_BACKEND_HINT = (
    "Scaffold an Express + TypeScript HTTP API in this target's directory: package.json "
    "(PIN exact versions), tsconfig.json compiling src/ to dist/, and src/server.ts that "
    "listens on process.env.PORT||3000, exposes GET /health -> 200 {\"ok\":true}, and "
    "implements every endpoint in the spec. Build with `tsc` so `dist/server.js` exists. "
    "Enable permissive CORS so the frontend can call it in preview. "
    "PERSISTENCE: if this target's detail has `datastore`, read the connection URL from "
    "process.env[detail.conn_env] (default DATABASE_URL) and use the matching driver "
    "(pg for postgres, mongodb for mongo) — PIN its exact version. If detail has "
    "`persist_path` instead, store data in a SQLite file at that path (e.g. better-sqlite3, "
    "PINNED). On boot, create the schema / run migrations IDEMPOTENTLY (CREATE TABLE IF NOT "
    "EXISTS or equivalent) so a restart against existing data does not fail. Never keep "
    "state only in memory when persistence is required."
)

_STATIC_HINT = (
    "This target is a PLAIN STATIC SITE: hand-written HTML, CSS and JavaScript served as "
    "files. There is no bundler, no package.json, no transpiler and no framework — do not "
    "introduce one, and do not add a dependency manifest. `index.html` at the target root is "
    "the entry point; keep styles and scripts in their own files and reference them with "
    "relative paths so the site works when served from any directory.\n"
    "Use modern browser APIs directly (ES modules, fetch, CSS custom properties, grid/flex) — "
    "the target is current browsers, so no polyfills or build step are needed.\n"
    "DESIGN — when adopting an existing page, the existing look IS the design: match its "
    "palette, type, spacing and markup conventions, and extend them rather than restyling. "
    "For anything new, hold the same quality floor: responsive down to mobile, every "
    "interactive control with explicit high-contrast colors plus visible hover AND "
    "keyboard-focus states, `prefers-reduced-motion` respected, real semantic labels/aria, "
    "and UI copy in plain active voice that says exactly what a control does."
)

# yarn 1.x queries its OWN registry for metadata even when .npmrc names a mirror, then fetches
# tarballs from the host recorded in the lockfile. Both have to be reachable, and the failure is
# confusing without this: the error names the lockfile's tarball URL while the CONNECT that was
# refused is yarn's registry.
NODE_YARN = Toolchain(image=NODE.image,
                      egress_hosts=("registry.yarnpkg.com", ".yarnpkg.com"))

# One Maven recipe per Java LTS: the build JDK must match the Java level the project declares,
# or it compiles against the wrong language level and library APIs. The adopt survey reads the
# declared level and picks the matching recipe.
MAVEN_JDKS = (8, 11, 17, 21)
_MAVEN_HOSTS = ("repo.maven.apache.org", "repo1.maven.org")

_MAVEN_HINT = (
    "This target is a Maven module in a multi-module Spring Boot project. It inherits from the "
    "parent POM and may depend on sibling modules, so it is built from the REACTOR ROOT with "
    "`-pl <module> -am`, never from inside the module directory.\n"
    "Keep the project's declared Java level (source/target/release in the POM) — do not raise "
    "it and do not use language features above it.\n"
    "If the repo carries a settings.xml, the build must use it (`-s settings.xml`): it usually "
    "redirects dependency resolution to an internal mirror, and without it the build reaches for "
    "Maven Central and fails in a restricted network."
)

# Built from the reactor root so the parent POM and sibling modules resolve; `-am` builds the
# sibling modules the target needs. The local repo goes inside the writable mount because the
# container runs as uid 1000 with no writable HOME. The JVM ignores HTTP_PROXY/HTTPS_PROXY (it
# needs -Dhttp.proxyHost/-Dhttp.proxyPort), so a contained build behind the egress proxy reaches
# the network only if the command translates them; without that Maven reports "Network is
# unreachable" against a host that is in fact allowlisted.
_MAVEN_BUILD = (
    'P="${HTTP_PROXY:-$http_proxy}"; PX=""; '
    'if [ -n "$P" ]; then H="${P#*//}"; H="${H%%/*}"; '
    'PX="-Dhttp.proxyHost=${H%%:*} -Dhttp.proxyPort=${H##*:} '
    '-Dhttps.proxyHost=${H%%:*} -Dhttps.proxyPort=${H##*:} '
    '-Dhttp.nonProxyHosts=localhost|127.0.0.1"; fi; '
    'mvn -B -q -DskipTests -Dmaven.repo.local=/out/.m2 $PX '
    '$([ -f settings.xml ] && echo -s settings.xml) '
    '-pl {name} -am package')

_VITE_HINT = (
    "This target is a Vite app that already has a lockfile. Install with the package manager "
    "that lockfile belongs to (yarn.lock -> yarn, package-lock.json -> npm); another manager "
    "cannot reproduce the locked install.\n"
    "Keep the existing env files and `--mode` build scripts and do not introduce a new build "
    "pipeline: one repo can define several builds through its modes.\n"
    "If a .npmrc names a registry, it is there for a reason (a restricted network); leave it in "
    "place."
)

REGISTRY: dict[str, Recipe] = {
    "node-vite-yarn": Recipe(
        name="node-vite-yarn", type="frontend", toolchain=NODE_YARN,
        scaffold_hint=_VITE_HINT,
        # yarn 1.x ships in the image already; `corepack enable` fails as uid 1000 (EACCES
        # unlinking /usr/local/bin/pnpm) and, chained with &&, took the whole build down.
        build_cmd="yarn install --frozen-lockfile && yarn build",
        artifact_glob="dist/**/index.html",
        boot=None,
        supported_checks=("route_status", "selector_present", "mobile_fit"),
    ),
    "node-vite-npm": Recipe(
        name="node-vite-npm", type="frontend", toolchain=NODE,
        scaffold_hint=_VITE_HINT,
        build_cmd="npm ci && npm run build",
        artifact_glob="dist/**/index.html",
        boot=None,
        supported_checks=("route_status", "selector_present", "mobile_fit"),
    ),
    "static-web": Recipe(
        name="static-web", type="frontend", toolchain=NODE,
        scaffold_hint=_STATIC_HINT,
        # There is nothing to compile, but verify still runs `rm -rf dist && <build_cmd>` and
        # then insists the artifact reappear — so the "build" stages the site into dist/. That
        # keeps the prove-the-build-is-real property (a stale dist cannot pass), gives deploy
        # the <target>/dist it serves, and leaves dist/ generated so publishing skips it.
        build_cmd=("mkdir -p dist && find . -mindepth 1 -maxdepth 1 "
                   "! -name dist ! -name node_modules ! -name .git "
                   "-exec cp -R {} dist/ \\;"),
        artifact_glob="dist/index.html",
        boot=None,
        supported_checks=("route_status", "selector_present", "mobile_fit"),
    ),
    "node-vite-react": Recipe(
        name="node-vite-react", type="frontend", toolchain=NODE,
        scaffold_hint=_FRONTEND_HINT,
        build_cmd="pnpm install --frozen-lockfile && pnpm build",
        artifact_glob="dist/index.html",
        boot=None,
        supported_checks=("route_status", "selector_present", "mobile_fit"),
    ),
    "node-express": Recipe(
        name="node-express", type="backend", toolchain=NODE,
        scaffold_hint=_BACKEND_HINT,
        build_cmd="pnpm install --frozen-lockfile && pnpm build",
        artifact_glob="dist/server.js",
        boot=BootSpec(cmd=("node", "dist/server.js"), port=3000, health_path="/health"),
        supported_checks=("api_json", "route_status", "persistence_survives_restart"),
    ),
    "postgres": Recipe(
        name="postgres", type="datastore", toolchain=NODE, kind="service",
        service=ServiceSpec(
            image="postgres:16-alpine", port=5432,
            env=(("POSTGRES_USER", "devagent"),
                 ("POSTGRES_PASSWORD", "devagent"),
                 ("POSTGRES_DB", "app")),
            volume_path="/var/lib/postgresql/data",
            ready_cmd=("pg_isready", "-U", "devagent"),
            conn_url_template="postgresql://devagent:devagent@{host}:{port}/app",
        ),
    ),
    "mongo": Recipe(
        name="mongo", type="datastore", toolchain=NODE, kind="service",
        service=ServiceSpec(
            image="mongo:7", port=27017,
            env=(),
            volume_path="/data/db",
            ready_cmd=("mongosh", "--quiet", "--eval", "db.runCommand({ping:1})"),
            conn_url_template="mongodb://{host}:{port}/app",
        ),
    ),
}

REGISTRY.update({
    f"maven-spring-boot-jdk{v}": Recipe(
        name=f"maven-spring-boot-jdk{v}", type="backend",
        toolchain=Toolchain(image=f"maven:3.9-eclipse-temurin-{v}", egress_hosts=_MAVEN_HOSTS),
        scaffold_hint=_MAVEN_HINT, build_cmd=_MAVEN_BUILD,
        artifact_glob="{name}/target/*.jar", boot=None,
        supported_checks=("route_status", "api_json"),
    ) for v in MAVEN_JDKS})

# Recipes for stacks found in an existing repo. They are not offered when scoping a new build.
ADOPT_ONLY = frozenset({f"maven-spring-boot-jdk{v}" for v in MAVEN_JDKS}
                       | {"node-vite-yarn", "node-vite-npm", "static-web"})


def load_external_recipes(directory) -> dict[str, Recipe]:
    """Parse every `*.json` recipe manifest under *directory* into Recipes. Each file is one
    recipe dict or a list of them. Raises ValueError (naming the file) on a malformed manifest —
    a typo'd recipe must fail loudly, not vanish. Returns {} if the dir is absent."""
    out: dict[str, Recipe] = {}
    p = Path(directory)
    if not p.is_dir():
        return out
    for f in sorted(p.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"recipe manifest {f}: invalid JSON: {e}") from e
        for item in (data if isinstance(data, list) else [data]):
            try:
                r = recipe_from_dict(item)
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"recipe manifest {f}: invalid recipe "
                                 f"{item.get('name', '?') if isinstance(item, dict) else item!r}: {e}") from e
            out[r.name] = r
    return out


# Merge operator-supplied recipes at import: a new stack is a manifest, not a code change.
_EXTERNAL_DIR = os.getenv("DEVAGENT_RECIPES_DIR")
if _EXTERNAL_DIR:
    REGISTRY.update(load_external_recipes(_EXTERNAL_DIR))


def get(name: str) -> Recipe:
    return REGISTRY[name]


def is_registered(name: str) -> bool:
    return name in REGISTRY
