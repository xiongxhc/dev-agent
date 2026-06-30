# devagent/phases/scope.py
"""Scope phase — the flexible entry point. Turns ANY request into a confirmed ProjectScope.

Classifies the request into one or more targets (each a registered recipe), and emits
clarifying questions when the request is underspecified. Questions go OUT over the notifier
(Feishu by default); answers come back via an answers file the operator supplies on re-run
(feishu.py is outbound-only, so there is no Feishu inbound here). When clarifications remain,
the phase exits 1 so ScopeGate stops the run until answers arrive."""

from .. import recipes
from ..llm import generate_structured
from ..schema import ProjectScope
from .base import PhaseContext, PhaseResult

_PROMPT = """\
You are scoping a software build request into a structured, buildable ProjectScope.

Decide WHAT to build — do NOT assume it is a web frontend. It may be backend-only, a
frontend, a fullstack app, or several targets that work together. Each target needs a
`type` and a `stack`, where `stack` MUST be one of these registered recipes:

{recipes}

Rules:
- One ArtifactSpec per deliverable. `name` is a short dir name (e.g. "web", "api").
- `type` MUST match the chosen recipe's type.
- acceptance_checks MUST be machine-checkable and use only the kinds the recipe supports:
{checks}
  For api_json, `route` is the HTTP path and `json_path` is a dotted path into the JSON body
  (e.g. "0.id" or "data.count"). For route_status, assert a path returns a status.
- If a fullstack app, point the frontend at the backend (note the base URL in the frontend
  target's detail).
- If the request is genuinely underspecified (you cannot pick targets/stacks/checks with
  confidence), put concrete questions in `clarifications` and leave targets as your best guess.
  If it is clear, leave `clarifications` empty.
- Persistence is your call. If the spec needs durable state, either (a) persist to a file
  inside the target (e.g. SQLite at `data/<x>.db`) — declare `detail.persist_path` on that
  backend, no extra target — or (b) add a datastore target (stack `postgres` or `mongo`,
  type `datastore`) and on the dependent backend set `detail.datastore` to the datastore
  target's `name` and `detail.conn_env` to the env var your code reads the connection URL
  from (default `DATABASE_URL`). Either way add a `persistence_survives_restart` acceptance
  check on the backend: set `route`+`method`+`body` for the write that creates a record,
  `json_path` to locate the created id in the response, and `verify_route` for the GET that
  must still return it after the API process restarts. A `datastore` target carries NO
  acceptance checks of its own.
- If any route requires authentication (login/token), declare an `auth` flow on that backend
  target: `auth.login_route` (+ `login_method`/`login_body` credentials) and `auth.token_json_path`
  (the dotted path to the token in the login response, e.g. "token"); add `auth.register_route`
  (+ `register_body`) if a user must be created first. Then set `auth: true` on every acceptance
  check that hits a protected route — the runner logs in ONCE and sends the token automatically.
  Leave `auth: false` (default) on checks that test the UNauthenticated case (e.g. a protected
  route returning 401 with no token). A persistence check on a protected route also needs `auth: true`.

REQUEST:
{request}
{answers}
"""


def _recipe_catalog() -> tuple[str, str]:
    lines, checks = [], set()
    for r in recipes.REGISTRY.values():
        lines.append(f"  - {r.name} (type={r.type}, checks={list(r.supported_checks)})")
        checks.update(r.supported_checks)
    return "\n".join(lines), "  " + ", ".join(sorted(checks))


class ScopePhase:
    name = "scope"

    def __init__(self, input_path: str, answers_path: str | None = None,
                 notifier=None, client=None):
        self.input_path = input_path
        self.answers_path = answers_path
        self.notifier = notifier          # callable(text)->None; None => best-effort Feishu
        self.client = client              # injectable anthropic client for tests

    def run(self, ctx: PhaseContext) -> PhaseResult:
        try:
            with open(self.input_path) as f:
                request = f.read()
            answers = ""
            if self.answers_path:
                with open(self.answers_path) as f:
                    answers_text = f.read()
                answers = "\nOPERATOR ANSWERS TO PRIOR QUESTIONS:\n" + answers_text
            cat, checks = _recipe_catalog()
            prompt = _PROMPT.format(recipes=cat, checks=checks, request=request, answers=answers)
            scope, usage = generate_structured(prompt, ProjectScope, client=self.client)

            if scope.clarifications:
                self._ask(scope.clarifications)
                return PhaseResult(name=self.name, exit_code=1,
                                   output="needs clarification", meta=usage,
                                   output_artifact=scope)
            return PhaseResult(name=self.name, exit_code=0, output=scope.title,
                               meta=usage, output_artifact=scope)
        except Exception as e:
            return PhaseResult(name=self.name, exit_code=1, output=str(e))

    def _ask(self, questions: list[str]) -> None:
        text = "DevAgent needs clarification before building:\n" + \
               "\n".join(f"- {q}" for q in questions) + \
               "\n\nReply by re-running with --answers <file>."
        notifier = self.notifier
        if notifier is None:
            try:
                from ..channels.feishu import notify_text as notifier  # best-effort
            except Exception:
                return
        try:
            notifier(text)
        except Exception:
            pass  # notification failure must not crash the phase
