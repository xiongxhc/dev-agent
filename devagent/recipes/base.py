# devagent/recipes/base.py
"""A Recipe is the curated, reliable know-how for building ONE artifact type: how to
scaffold it, build it from source, prove the build is real, boot it for acceptance, and
which sandbox toolchain it needs. The Scope phase SELECTS a recipe by name (== ArtifactSpec
.stack); it never lets the model invent a toolchain."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Toolchain:
    image: str                                   # sandbox image carrying the toolchain
    egress_hosts: tuple[str, ...] = ()           # extra allowlist hosts beyond base npm+anthropic
    dockerfile: str | None = None                # Dockerfile that BUILDS `image` (path relative to the
                                                 # manifest dir) — lets a manifest ship a NEW toolchain as
                                                 # data; `build.sh recipes` builds it (M11). None = prebuilt.
    build_context: str | None = None             # docker build context (default: the Dockerfile's dir)


@dataclass(frozen=True)
class BootSpec:
    """How to start a service for acceptance (None on a Recipe means 'static, no boot')."""

    cmd: tuple[str, ...]                          # e.g. ("node", "dist/server.js")
    port: int                                    # the port the service listens on
    health_path: str = "/health"                 # poll this for readiness
    ready_timeout_s: float = 30.0


@dataclass(frozen=True)
class ServiceSpec:
    """Everything needed to run a stock datastore image as a sibling container — no source,
    no build. Carried by a kind=="service" Recipe; consumed by verify (sibling bring-up) and
    deploy (service branch)."""

    image: str                        # e.g. "postgres:16-alpine"
    port: int                         # in-container port, e.g. 5432
    env: tuple[tuple[str, str], ...]  # image config, e.g. (("POSTGRES_USER","devagent"),)
    volume_path: str                  # container dir to persist, e.g. "/var/lib/postgresql/data"
    ready_cmd: tuple[str, ...]        # exec'd in the container for readiness, e.g. ("pg_isready",)
    conn_url_template: str            # "postgresql://devagent:devagent@{host}:{port}/app"
    ready_timeout_s: float = 60.0


@dataclass(frozen=True)
class Recipe:
    name: str                                    # == ArtifactSpec.stack, e.g. "node-express"
    type: str                                    # == ArtifactSpec.type, e.g. "backend"
    toolchain: Toolchain
    scaffold_hint: str = ""                       # build-prompt fragment (build recipes only)
    build_cmd: str = ""                           # run in the target dir (build recipes only)
    artifact_glob: str = ""                       # proof of a real build (build recipes only)
    boot: BootSpec | None = None
    supported_checks: tuple[str, ...] = ()
    kind: str = "build"                           # "build" | "service"
    service: ServiceSpec | None = None            # set iff kind == "service"


def recipe_from_dict(d: dict) -> Recipe:
    """Build a Recipe from a plain dict (a JSON manifest) — the seam that lets an operator add a
    new stack/language as DATA (a recipe file) instead of editing this module. Raises KeyError/
    ValueError on a malformed manifest so a bad recipe fails loudly at load, never silently."""
    tc = d.get("toolchain") or {}
    if "image" not in tc:
        raise ValueError("recipe.toolchain.image is required")
    toolchain = Toolchain(image=tc["image"], egress_hosts=tuple(tc.get("egress_hosts", ())),
                          dockerfile=tc.get("dockerfile"), build_context=tc.get("build_context"))

    boot = None
    if d.get("boot"):
        b = d["boot"]
        boot = BootSpec(cmd=tuple(b["cmd"]), port=int(b["port"]),
                        health_path=b.get("health_path", "/health"),
                        ready_timeout_s=float(b.get("ready_timeout_s", 30.0)))

    service = None
    if d.get("service"):
        s = d["service"]
        env = s.get("env") or {}
        env_pairs = tuple(env.items()) if isinstance(env, dict) else tuple(tuple(p) for p in env)
        service = ServiceSpec(
            image=s["image"], port=int(s["port"]), env=env_pairs,
            volume_path=s["volume_path"], ready_cmd=tuple(s["ready_cmd"]),
            conn_url_template=s["conn_url_template"],
            ready_timeout_s=float(s.get("ready_timeout_s", 60.0)))

    return Recipe(
        name=d["name"], type=d["type"], toolchain=toolchain,
        scaffold_hint=d.get("scaffold_hint", ""), build_cmd=d.get("build_cmd", ""),
        artifact_glob=d.get("artifact_glob", ""), boot=boot,
        supported_checks=tuple(d.get("supported_checks", ())),
        kind=d.get("kind", "build"), service=service)
