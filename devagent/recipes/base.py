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
