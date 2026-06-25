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
class Recipe:
    name: str                                    # == ArtifactSpec.stack, e.g. "node-express"
    type: str                                    # == ArtifactSpec.type, e.g. "backend"
    toolchain: Toolchain
    scaffold_hint: str                           # build-prompt fragment
    build_cmd: str                               # run in the target dir
    artifact_glob: str                           # proof of a real build, relative to target dir
    boot: BootSpec | None
    supported_checks: tuple[str, ...] = ()
