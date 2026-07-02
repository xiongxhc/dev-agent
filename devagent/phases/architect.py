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
