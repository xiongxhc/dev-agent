"""Typed, validated artifacts that flow between phases: Scope → Plan → Build.

These are the frozen contracts the Executor seam consumes. `ProjectScope` is the
confirmed, flexible contract produced by the Scope phase; it may contain one or more
`ArtifactSpec` targets (frontend, backend, CLI, or any other registered recipe type).
Every acceptance check in each `ArtifactSpec` is machine-checkable: `route_status`,
`selector_present`, `api_json`, `command_exit`, or `stdout_matches` — the verify phase
computes a bool from each without the model in the loop. The stack is OPEN (no frozen
enum); `ScopeGate` validates each target's stack against the recipe registry."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Auth-style vocabulary the verify runner can execute. OPEN like the recipe registry: the
# runner dispatches on this string (acceptance_runner._CRED_BUILDERS), so a new style is a
# dispatch-table entry, never per-app code (M10 depth; M11 "declarative auth styles").
KNOWN_AUTH_MODES = ("bearer", "cookie")

AcceptanceKind = Literal[
    "route_status", "selector_present",   # frontend (HTTP / browser)
    "api_json",                            # backend (HTTP JSON-body assertion)
    "command_exit", "stdout_matches",      # cli/mcp (subprocess) — plumbed for future recipes
    "persistence_survives_restart",        # backend durability across an app restart
]


class AcceptanceCheck(BaseModel):
    """A single machine-checkable acceptance criterion. Required fields depend on `kind`
    (enforced below) so every check yields a deterministic bool with no model in the loop."""

    model_config = ConfigDict(populate_by_name=True)

    kind: AcceptanceKind
    # HTTP checks (route_status / selector_present / api_json):
    route: str | None = None
    expected_status: int = 200
    selector: str | None = None                 # selector_present
    json_path: str | None = None                # api_json: dotted path, e.g. "data.0.id"
    json_equals: Any = None                     # api_json: expected value at json_path (None = presence-only)
    method: str = "GET"                         # api_json HTTP method
    body: dict | None = None                    # api_json request body
    verify_route: str | None = None             # persistence_survives_restart: GET path to read state back
    auth: bool = False                          # send the target's default AuthFlow credential on this check
    as_actor: str | None = Field(default=None, alias="as")  # authz: run this check AS this named actor
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
        if self.kind == "persistence_survives_restart":
            if not self.route or not self.route.startswith("/"):
                raise ValueError("persistence_survives_restart requires a route starting with '/'")
            if not self.verify_route or not self.verify_route.startswith("/"):
                raise ValueError("persistence_survives_restart requires a verify_route starting with '/'")
            if not self.json_path:
                raise ValueError("persistence_survives_restart requires json_path to locate the created id")
        return self


class AuthFlow(BaseModel):
    """How the verify harness obtains a credential for auth-protected checks. The runner logs
    in ONCE per flow and sends the credential on every check that names it. Deterministic: the
    runner executes the flow, it never judges pass/fail with the model.

    `mode` selects the credential style (bearer token vs captured session cookie). A single
    flow can be the target's default (referenced by `auth=True` checks); a list of named
    flows (`ArtifactSpec.actors`, each with a `name`/`role`) forms a permission matrix that
    checks reference by `as: <name>`."""

    login_route: str                            # e.g. "/auth/login"
    login_method: str = "POST"
    login_body: dict = Field(default_factory=dict)   # credentials to POST at login
    token_json_path: str = ""                   # bearer: dotted path to the token, e.g. "token" (unused for cookie)
    header: str = "Authorization"               # bearer: header the token rides in
    scheme: str = "Bearer"                      # bearer: header value is f"{scheme} {token}".strip()
    mode: str = "bearer"                        # "bearer" | "cookie" — the credential style (KNOWN_AUTH_MODES)
    name: str | None = None                     # actor name for an authz matrix (None = the target's default flow)
    role: str | None = None                     # informational role label, surfaced to the build prompt
    register_route: str | None = None           # optional: create the user before logging in
    register_method: str = "POST"
    register_body: dict | None = None           # body to register with (defaults to login_body)

    @model_validator(mode="after")
    def _validate(self):
        for r in (self.login_route, self.register_route):
            if r is not None and not r.startswith("/"):
                raise ValueError("AuthFlow routes must start with '/'")
        if self.mode not in KNOWN_AUTH_MODES:
            raise ValueError(f"AuthFlow.mode {self.mode!r} must be one of {KNOWN_AUTH_MODES}")
        if self.mode == "bearer" and not self.token_json_path:
            raise ValueError("AuthFlow bearer mode requires token_json_path")
        return self


class ArtifactSpec(BaseModel):
    """One deliverable in a project. `type`/`stack` are OPEN strings (gated against the
    recipe registry by ScopeGate), so the model can classify any request without a frozen
    enum. `detail` is the type-specific shape (frontend: pages/components; backend: endpoints)."""

    type: str = Field(..., min_length=1)        # "frontend" | "backend" | ...
    stack: str = Field(..., min_length=1)        # recipe name, e.g. "node-express"
    name: str = Field(..., min_length=1)         # target dir/name, e.g. "web", "api"
    detail: dict = Field(default_factory=dict)
    auth: AuthFlow | None = None                 # the default flow (referenced by `auth=True` checks)
    actors: list[AuthFlow] = Field(default_factory=list)  # named flows forming an authz permission matrix
    acceptance_checks: list[AcceptanceCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _auth_checks_have_a_flow(self):
        if any(c.auth for c in self.acceptance_checks) and self.auth is None:
            raise ValueError(
                "a check sets auth=True but the target declares no `auth` flow — "
                "the runner would have no credential to send"
            )
        named = [a.name for a in self.actors if a.name]
        if any(a.name is None for a in self.actors):
            raise ValueError("each actor in `actors` must declare a name")
        if len(named) != len(set(named)):
            raise ValueError("actors must have unique names")
        actor_names = set(named)
        for c in self.acceptance_checks:
            if c.as_actor is not None and c.as_actor not in actor_names:
                raise ValueError(
                    f"a check runs `as: {c.as_actor}` but no actor with that name is declared in `actors`"
                )
        return self


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
