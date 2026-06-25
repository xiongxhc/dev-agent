# devagent/recipes/registry.py
"""The recipe REGISTRY. M6 registers two Node recipes (no new image). Adding a recipe
makes a new artifact type buildable with NO harness change — that is the extensibility
seam M7 (Java/Python, brownfield detection) plugs into."""

from .base import BootSpec, Recipe, Toolchain

NODE = Toolchain(image="devagent-sandbox:m2")    # node + pnpm + chromium + python3; npm allowlist

_FRONTEND_HINT = (
    "Scaffold a Vite + React + Tailwind app in this target's directory: package.json "
    "(PIN exact dependency versions — no ^ or ~), vite.config, tailwind.config, "
    "postcss.config, index.html, and src/. Implement every page (a route that renders) "
    "and satisfy every acceptance check. If the spec lists a backend base URL, read it "
    "from an env var (import.meta.env.VITE_API_BASE) defaulting to that URL."
)
_BACKEND_HINT = (
    "Scaffold an Express + TypeScript HTTP API in this target's directory: package.json "
    "(PIN exact versions), tsconfig.json compiling src/ to dist/, and src/server.ts that "
    "listens on process.env.PORT||3000, exposes GET /health -> 200 {\"ok\":true}, and "
    "implements every endpoint in the spec. Build with `tsc` so `dist/server.js` exists. "
    "Enable permissive CORS so the frontend can call it in preview."
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
        supported_checks=("api_json", "route_status"),
    ),
}


def get(name: str) -> Recipe:
    return REGISTRY[name]


def is_registered(name: str) -> bool:
    return name in REGISTRY
