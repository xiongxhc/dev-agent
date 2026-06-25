"""Typed, validated artifacts that flow between phases: Brief -> Spec -> Plan.

These are the frozen contracts the Executor seam consumes. The defining property of a
Spec is that EVERY acceptance check is machine-checkable (a route+status, or a
selector on a route) — the verify phase computes a bool from each without the model.
The opinionated stack is fixed here (research Q1): the model fills a Spec, it does not
choose a framework."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# The one blessed stack (research Q1). The scaffold template matches this.
STACK = "vite-react-tailwind"

AcceptanceKind = Literal[
    "route_status", "selector_present",   # frontend (HTTP / browser)
    "api_json",                            # backend (HTTP JSON-body assertion)
    "command_exit", "stdout_matches",      # cli/mcp (subprocess) — plumbed for future recipes
]


class AcceptanceCheck(BaseModel):
    """A single machine-checkable acceptance criterion. Required fields depend on `kind`
    (enforced below) so every check yields a deterministic bool with no model in the loop."""

    kind: AcceptanceKind
    # HTTP checks (route_status / selector_present / api_json):
    route: str | None = None
    expected_status: int = 200
    selector: str | None = None                 # selector_present
    json_path: str | None = None                # api_json: dotted path, e.g. "data.0.id"
    json_equals: Any = None                     # api_json: expected value at json_path (None = presence-only)
    method: str = "GET"                         # api_json HTTP method
    body: dict | None = None                    # api_json request body
    # subprocess checks (command_exit / stdout_matches):
    argv: list[str] | None = None
    expected_exit: int = 0
    pattern: str | None = None                  # stdout_matches: regex

    @model_validator(mode="after")
    def _required_per_kind(self):
        http_kinds = {"route_status", "selector_present", "api_json"}
        if self.kind in http_kinds:
            if not self.route or not self.route.startswith("/"):
                raise ValueError(f"{self.kind} requires a route starting with '/'")
        if self.kind == "selector_present" and not self.selector:
            raise ValueError("selector_present requires a non-empty selector")
        if self.kind in {"command_exit", "stdout_matches"} and not self.argv:
            raise ValueError(f"{self.kind} requires a non-empty argv")
        if self.kind == "stdout_matches" and not self.pattern:
            raise ValueError("stdout_matches requires a pattern")
        return self


class ArtifactSpec(BaseModel):
    """One deliverable in a project. `type`/`stack` are OPEN strings (gated against the
    recipe registry by ScopeGate), so the model can classify any request without a frozen
    enum. `detail` is the type-specific shape (frontend: pages/components; backend: endpoints)."""

    type: str = Field(..., min_length=1)        # "frontend" | "backend" | ...
    stack: str = Field(..., min_length=1)        # recipe name, e.g. "node-express"
    name: str = Field(..., min_length=1)         # target dir/name, e.g. "web", "api"
    detail: dict = Field(default_factory=dict)
    acceptance_checks: list[AcceptanceCheck] = Field(default_factory=list)


class RepoBinding(BaseModel):
    """Where built output goes. M7 fills this in; M6 only carries the flag."""

    mode: Literal["none", "new", "existing"] = "none"
    url: str | None = None


class ProjectScope(BaseModel):
    """The confirmed, flexible contract the whole pipeline consumes. Replaces the
    frontend-only Spec as the artifact emitted by the Scope phase."""

    title: str = Field(..., min_length=1)
    targets: list[ArtifactSpec] = Field(..., min_length=1)
    repo: RepoBinding | None = None
    clarifications: list[str] = Field(default_factory=list)  # open questions; empty = confirmed


class Brief(BaseModel):
    """Normalized intake output."""

    source: Literal["prd", "url"]
    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    requirements: list[str] = Field(default_factory=list)


class Spec(BaseModel):
    title: str = Field(..., min_length=1)
    stack: Literal["vite-react-tailwind"] = STACK
    pages: list[str] = Field(..., min_length=1)
    components: list[str] = Field(default_factory=list)
    acceptance_checks: list[AcceptanceCheck] = Field(..., min_length=1)


class Task(BaseModel):
    id: str
    description: str = Field(..., min_length=1)
    owned_files: list[str] = Field(..., min_length=1)  # disjoint across tasks (see Plan)


class Plan(BaseModel):
    tasks: list[Task] = Field(..., min_length=1)

    @field_validator("tasks")
    @classmethod
    def _file_ownership_is_disjoint(cls, tasks):
        seen: dict[str, str] = {}
        for t in tasks:
            for f in t.owned_files:
                if f in seen:
                    raise ValueError(
                        f"file {f!r} owned by both task {seen[f]!r} and {t.id!r} "
                        "— build subagents would collide"
                    )
                seen[f] = t.id
        return tasks
