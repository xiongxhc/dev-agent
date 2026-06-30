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
    "runtime without a rebuild)."
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

REGISTRY: dict[str, Recipe] = {
    "node-vite-react": Recipe(
        name="node-vite-react", type="frontend", toolchain=NODE,
        scaffold_hint=_FRONTEND_HINT,
        build_cmd="pnpm install --frozen-lockfile && pnpm build",
        artifact_glob="dist/index.html",
        boot=None,
        supported_checks=("route_status", "selector_present"),
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
