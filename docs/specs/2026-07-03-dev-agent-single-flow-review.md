# Dev-agent pipeline review: why `build-system` fails where `run --build` succeeds — and the case for one flow

**Date:** 2026-07-03. **Trigger:** 4/4 Team Polls `build-system` runs failed integration while
Feishu single-runs (Team Todos, calendar-todos) shipped working apps in one pass each.
**Verdict up front:** the two lanes should collapse into one. The system lane's failures are not
bad luck — they are structural, and every one traces to the same root: **the design is decided
more than once, by LLM calls that cannot see each other.**

---

## 1. What actually differs between the lanes

| | single-run (`run --build`, Feishu) | system (`build-system`) |
|---|---|---|
| Design authority | ONE ScopePhase scopes db+api+web together | Architect designs; then **each service re-scopes itself** |
| Check generators | 1 (scope's acceptance checks, self-consistent) | **3 independent**: architect `integration_checks`, each sub-run's scope `acceptance_checks`, contract-derived probes |
| Contracts | n/a (one LLM context sees everything) | Frozen OpenAPI/db-schema — but **advisory text only**; checks are not derived from them |
| Build context | one executor run, whole app | one executor run **per service**, blind to siblings |
| Deploy | preview URL, gated on responding | **none** — bring-up + E2E, then teardown; success leaves nothing running |
| Observed result | Team Todos ✅, calendar-todos ✅ | Team Polls ❌×4 (`integration_failed`) |

Cost/time, same product shape (web+api+db):

- calendar-todos, single-run: **$1.69, ~6 min**, preview live, working.
- Team Todos, single-run (after resume): **$1.04, ~4 min**, preview live, working.
- Team Polls, system run `run-1783072308`: api $1.68/435s + web $1.29/312s + unlogged
  architect cost ≈ **$3+, ~15 min, integration_failed, nothing deployed**. Four such runs
  today ≈ **$10–12 spent, zero working product**.

## 2. The smoking gun: three specs for one route

For `GET /polls`, run `run-1783072308` demanded, simultaneously:

1. **Frozen contract** (`design.json`, `openapi_polls`, produced by the architect):
   200 response is a **bare array** of `{id, question, options[{id,text,votes}]}`.
2. **Architect's integration checks** (same design.json): `json_path: "0.question"`,
   `"0.options.0.votes"` — also array-shaped. Consistent with (1).
3. **The api sub-run's own scope** (`services/api/out/.devagent/scope.json`), generated
   by ScopePhase from the prd_slice **without ever seeing the contract**:
   `api_json /polls json_path: "polls"` — an **object** `{"polls": [...]}`. And for the vote
   route: `json_path: "success"` where the contract says the response is the option with
   `votes`. Contradicts (1) and (2).

No response shape satisfies both check sets. The in-container acceptance run enforces (3);
the post-build integration run enforces (2). The builder — correctly optimizing for the checks
it is judged by — produced the hybrid `{"length","count","polls","data",...}` object. It then
**passed its own acceptance 5/5 and failed the system's integration**, and the frontend
(built faithfully to the array contract, `Array.isArray()` guard) rendered an empty list
forever. That is the "created a poll but it never shows up" bug.

Why the blindness is structural, not a prompt bug:

- `devagent/phases/scope.py:111` — the scope prompt is `_PROMPT.format(recipes, checks,
  request, answers)`. **Contracts are not an input.** Sub-run scope invents checks from
  prd_slice prose alone.
- Contracts reach only the **build prompt** (`enrich_scope` → `_consumed/_provided_contracts`,
  `devagent/sdk_runner.py:82`). So the builder sees the contract, but is *graded* by checks
  that contradict it.
- `devagent/phases/architect.py` (71 lines) — integration checks are free-written by the
  architect LLM in the same call as the contracts. Today they happened to agree with the
  contract; nothing enforces that either.

## 3. Secondary defects found on the way (all system-lane)

1. **Sub-runs re-decide design.** The api sub-run's scope added its **own `db` datastore
   target** even though the design already has a `db` service node. Bring-up then starts BOTH
   (the design node un-namespaced, the node-internal one via `wire_targets` with `api-` prefix)
   with ambiguous `DATABASE_URL` wiring.
2. **Design-level datastore nodes get collision-prone names.** `system_build.py:133` calls
   `start_service(node, network=net)` without `container_name`, so the design db becomes
   `devagent-preview-db` — the SAME fixed name single-run previews use. Each caller
   `docker rm -f`s the other's container: a system bring-up and a live single-run preview
   clobber each other. (Today's calendar-todos preview db survived only by starting last.)
3. **Integration checks assert absolute state** (`id=1`, `votes=1`) against an environment
   whose freshness the pipeline does not actually guarantee (observed: `id=3 (want 1)`,
   `votes=2 (want 1)`). Fixture-thinking applied to dynamic systems.
4. **The ledger lies at the end.** `tree.py:126` logs `system_build_end status=succeeded`
   after the per-service builds — before integration. All three `integration_failed` runs have
   ledgers ending "succeeded"; the truth exists only in `system-report.json`. Architect
   cost/tokens are also absent from the ledger entirely.
5. **No deploy on success.** `_build_system` never deploys; even a green run tears everything
   down. Today's polls preview containers were brought up by hand.

## 4. Is the differentiation justified at all?

The design docs' rationale for the system lane: context-window scale, per-service git repos
(M7), long-horizon builds (M18/M19). Those are real *future* needs. But:

- The single-run pipeline **already builds multi-service products** (db+api+web) — both
  successful apps today are exactly that shape. "Has multiple services" is not what needs the
  system lane; "exceeds one build context" is, and nothing built today comes close.
- The system lane as wired gives you **three designers and no dictator**. Decomposition isn't
  the mistake; re-scoping inside each fragment is.

## 5. Recommendation: one flow

One pipeline: `scope → plan → build → verify → deploy`. "System" becomes a *scale
parameter* of that flow, not a second flow:

1. **Scope once.** For large PRDs, the architect IS the scope phase (one LLM decision:
   targets, stacks, contracts, checks). **Delete the per-service ScopePhase in sub-builds** —
   a sub-build receives its frozen target spec + contracts and goes straight to plan/build.
   This removes the entire class of contradiction fixed piecemeal today
   (`_contract_conformed_checks` and both morning route_status patches become dead code).
2. **One check set, derived from contracts.** Acceptance checks for contracted routes are
   generated **mechanically** from the OpenAPI spec (extend `contract_utils.openapi_to_checks`
   to walk response schemas: array root → `0.field` paths, etc.). The same checks run twice:
   in-container per service (acceptance) and against the brought-up system (integration).
   LLM-written checks survive only for uncontracted behavior.
3. **Deploy is deploy.** Bring-up already starts the real containers; on green, keep them and
   emit the preview URL exactly like single-run — same DeployGate, same ledger events.
4. **Per-run namespacing everywhere.** Design-level datastore nodes get run-scoped
   container/volume names like everything else; no cross-lane `rm -f` fratricide.
5. **Truthful ledger.** `system_build_end` moves after integration and carries the real
   status; architect cost is logged like any phase.

Sequencing: (2) is the highest value-per-line (kills the observed failure class outright);
(1) is the structural fix; (3)–(5) are small. After (1)+(2) there is exactly one flow with one
authority, and `build-system` survives only as "run the flow with an architect-sized scope."

## 5b. Implementation status (same day)

Recommendations 1–5 are IMPLEMENTED (2026-07-03 evening), full suite green (384 tests):
`scope_for_node` + `FrozenScopePhase` (design decided once, per-service ScopePhase removed
from the system lane); `derive_checks`/`derive_persistence_check`/`derive_integration_checks`
in contract_utils (one check set from the contract, in-container and post-bring-up; architect
prose checks are fallback only); bring-up keeps the system as the preview on success and
reports urls; design datastore nodes get per-run container names and sub-scope datastore
targets are not double-started; ledger's final `system_build_end` is post-integration truth
(tree verdict renamed `tree_build_end`), architect cost logged as a phase event.

## 6. Related state changes made alongside this review

- Feishu bot runs now land in `dev-agent/runs/` (same dir as CLI; `~/devagent-runs` moved
  in and removed; four preview containers recreated on the new paths; bot restarted).
- Band-aid (kept, still useful until fix 2 lands): `_contract_conformed_checks` now also
  drops `api_json` checks whose json_path root shape contradicts the provided contract's
  declared response schema (`devagent/executor.py`, test in `tests/test_executor.py`).
