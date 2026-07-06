# devagent/phases/architect.py
"""Architect phase (M14) — the design step above scope. Turns a PRD into a SystemDesign: a
dependency-ordered DAG of services plus the frozen contracts between them. The PRD states
REQUIREMENTS, not services — the Architect decides the decomposition, designing the services
the PRD does not name (research+spec on the unknown; it never stalls for a human). Downstream
(M15) builds each service as an isolated sub-run. Brain phase: Messages API forced tool-use
-> validated SystemDesign, same as ScopePhase."""

from ..llm import generate_structured
from ..schema import SystemDesign
from .base import PhaseContext, PhaseResult
from .scope import _recipe_catalog

_PROMPT = """\
You are the ARCHITECT. Turn the product requirements below into a SystemDesign: the set of
services that together deliver them, the dependency edges between services, and the contracts
(interfaces) each service provides and consumes. The requirements state WHAT is needed, not a
list of services — YOU decide the service decomposition.

Each service picks a `stack` from these registered recipes (its `kind` MUST equal the recipe's type):
{recipes}

Rules:
- One ServiceNode per independently-buildable service. `name` is a short dir name (services/<name>/).
- `prd_slice` is the portion of the requirements THIS service is responsible for — enough for a
  downstream builder to scope and build this service alone. Do NOT copy the whole PRD into every slice.
- Wire dependencies: if service A calls service B, put B's id in A.`depends_on`, and the contract
  B implements (e.g. its openapi) in B.`provides` AND A.`consumes` (the same contract id).
- Every Contract has a `producer` (the service id that implements it), and that producer MUST list
  the contract id in its `provides`. Anything a service `consumes` MUST be provided by a service it
  `depends_on`.
- A Contract `kind` is one of: openapi (an HTTP API), db_schema (a shared datastore's tables),
  auth_token (the identity/token shape), event (an async message).
- The dependency graph MUST be acyclic.
- Keep contracts minimal but real: for openapi put paths + request/response shapes in `spec`; for
  db_schema the tables/columns; for auth_token the claims.
- AUTH in openapi contracts (verification checks are DERIVED from these markers — omitting them
  means auth goes unverified):
  - Include the auth endpoints themselves in `paths` with request/response shapes: a register
    POST (accepting EXACTLY the signup fields — never a role field, roles are assigned
    server-side) and a login POST whose 200 response body contains a `token` property.
  - Mark every op that requires a credential with `security: [{{"bearerAuth": []}}]`. Protected
    routes return 401 without a token.
  - Mark ops restricted to a privileged role with `x-required-role: <role>` as well — a
    freshly-registered user is a REGULAR member, and a member hitting such an op gets 403.
- MOBILE / WebView requirements: adapting a UI for phones is a REDESIGN, not a squeeze — a
  desktop pattern that does not translate must be replaced by a mobile pattern in the SAME
  design language. When the requirements include mobile/WebView, the frontend `prd_slice`
  MUST contain: (a) a per-screen adaptation mapping naming the mobile pattern for each screen
  (wide table -> card list; sidebar nav -> bottom tab bar; hover actions -> visible buttons;
  side-by-side panels -> stacked navigation), and (b) a design-continuity rule: same design
  tokens (palette, typography, spacing, components) restyled for touch — never a new theme.
  The build is graded at a phone viewport: no horizontal overflow, 44px minimum touch targets.
- A service that CONSUMES another service's API must NOT rebuild it. Write its `prd_slice` so the
  downstream builder knows that API already exists and is supplied at runtime (e.g. a frontend
  slice says "the API exists — build ONLY the web UI against it"); otherwise the sub-build will
  scope a duplicate backend of its own.
- `integration` steps run against a FRESHLY BOOTED, EMPTY system, in order. A step may only
  assert on data an EARLIER step created — POST with a `body` first, then GET and assert.
  Health/readiness checks are always fine. The runner cannot carry values between steps, so only
  reference ids that are deterministic on a fresh database (the first created record is id 1).
  `json_path` is a plain dotted path into the response body ("ok", "0.question", "items.0.id");
  array indexes are numeric segments; NEVER JSONPath syntax ($, [*], filters).

REQUIREMENTS:
{request}
"""


class ArchitectPhase:
    name = "architect"

    def __init__(self, input_path: str, client=None):
        self.input_path = input_path
        self.client = client  # injectable anthropic client for tests

    def run(self, ctx: PhaseContext) -> PhaseResult:
        try:
            with open(self.input_path) as f:
                request = f.read()
            cat, _ = _recipe_catalog()
            prompt = _PROMPT.format(recipes=cat, request=request)
            design, usage = generate_structured(
                prompt, SystemDesign, client=self.client, max_tokens=8000)
            return PhaseResult(name=self.name, exit_code=0, output=design.title,
                               meta=usage, output_artifact=design)
        except Exception as e:
            return PhaseResult(name=self.name, exit_code=1, output=str(e))
