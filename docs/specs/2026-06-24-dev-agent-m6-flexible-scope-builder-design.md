# DevAgent M6 — Flexible Scope-First Builder (design)

**Date:** 2026-06-24
**Status:** approved design — ready for implementation plan
**Builds on:** [`2026-06-22-dev-agent-research-synthesis.md`](./2026-06-22-dev-agent-research-synthesis.md)
**Supersedes:** the single-stack assumption (research Q1) and the frontend-shaped `Spec`/`intake` of M2.

---

## 0. One-line conclusion

DevAgent stops assuming the answer (a Vite+React frontend) before it reads the question.
M6 inverts the pipeline: a **Scope phase** turns *any* request into a flexible, confirmed
`ProjectScope`, and everything downstream **dispatches on that scope** via an open,
extensible **recipe registry**, with the **toolchain provisioned per project**. The first
slice is proven end-to-end on a **fullstack frontend + backend app**.

---

## 1. Why (the corrections this design encodes)

This design is the product of a brainstorm that overturned several earlier assumptions.
Recording them so they are not re-introduced:

- **Execution order is M6 → M5 → M7** (decided 2026-06-24). The SDK-vs-Managed A/B (M5)
  was reordered to run *after* the full flow exists, so it measures the real workload, not
  a toy frontend. M6 makes both A/B arms multi-target-capable, which is *why* it precedes M5.
- **The deliverable is request-driven, not a fixed shape.** It is NOT "1 frontend + 1
  backend + 1 CLI". Depending on the request it can be backend-only, an MCP server instead
  of a CLI, frontend-only, a plugin, a Claude skill, a full product clone ("copy Airbnb"),
  in any language, **with or without a repo**. The system must be flexible.
- **The first real step is scope understanding + clarification**, before any build.
- **The toolchain is not fixed.** The old `devagent-sandbox:m2` image is an implementation
  detail, not a constraint. The sandbox/toolchain is **provisioned from what the scope
  needs**, and is re-derivable when the requirement is given.
- **New projects use a modern stack (not Java); older projects are Java/Spring Boot.** Java
  is therefore a *brownfield* problem (operate inside an existing repo), inseparable from
  git-binding → it lives in **M7**, not here. M6 is **greenfield modern**.
- **The architecture is open; M6's buildable set is not.** Recipes are added without
  touching the harness, so what's *possible* grows over time. But in M6 only types with a
  registered recipe are *buildable* — the scoper classifies any request, yet an unreciped
  type fails honestly ("no recipe yet") rather than half-building (verify needs recipe
  commands). Attempt-anyway is a future registry property, not M6 behavior.

## 2. Scope of M6

**In:** the flexible architecture (Scope phase + `ProjectScope` model + recipe registry +
per-project toolchain + multi-target build/verify/acceptance/deploy), shipped with **two
recipes** — `frontend` (refactor of today's behavior) and `backend` (new, Node/TS +
Express) — and proven by **one live fullstack build**.

**Out (deferred):**
- MCP / CLI / skill recipes, Python / Java backends → later recipes.
- Brownfield (existing-repo) builds, **git destination binding**, GitLab CI / k8s deploy,
  the `internal-ops-cli` ops platform (portal `192.0.2.5`) → **M7**.
- Reference-clone *recipe* ("copy Airbnb" as a buildable target) → later. The Scope phase
  may still *classify* such a request now; only the clone recipe is deferred.
- The SDK-vs-Managed A/B run → **M5**.

## 3. Architecture — the inverted pipeline

```
request (PRD file / "copy X" / "an MCP for Y" / URL)
   │
   ▼
SCOPE ── classify deliverable(s) + stack + repo? ──┐
   │      clarify ambiguity ⇄ operator (Feishu)    │  loop until confirmed
   ▼                                                │
ProjectScope  (flexible, open contract) ◀───────────┘
   │
   ▼   dispatch per target; toolchain provisioned from scope
[ recipe(frontend) ] [ recipe(backend) ] … (open, extensible)
   │
   ▼   per-target:  build → rebuild-from-source verify → boot → acceptance → repair (cap 2)
DEPLOY  (per-target local preview, wired together)  →  REPORT
```

The deterministic harness (`orchestrator.py`), gates-as-code, shared Budget/Ledger, and the
swappable `Executor` seam (SDK arm A / Managed arm B) are all **unchanged in spirit** — M6
generalizes the *artifacts that flow through them* and the *phases at the front and back*.

## 4. Components

### 4.1 Scope phase (`phases/scope.py`, new) — the heart

Replaces the frontend-shaped `intake` as the pipeline entry. Input: an arbitrary request.
Output: a confirmed `ProjectScope`.

Responsibilities:
1. **Classify the deliverable(s):** one or more artifact targets, each with a `type` and
   `stack` (both **open strings**, validated against the recipe registry — not a frozen
   enum), plus **repo-or-not**.
2. **Clarify ambiguity:** when the request is underspecified, emit targeted **clarifying
   questions** and pause for operator answers *before* the autonomous build begins. This
   keeps the build itself unattended (same up-front-confirmation principle M7 uses).
3. **Emit `ProjectScope`** once confirmed.

Implementation: a bounded Messages-API call (`llm.generate_structured`) producing a
validated `ProjectScope`, plus a clarification sub-step. A `ScopeGate` validates the scope
(every target's `type`/`stack` is buildable — has a recipe, or is explicitly flagged
"novel, attempt anyway").

**Clarification transport (asymmetric in M6 — this is a real constraint).** The existing
`channels/feishu.py` is a custom-bot **incoming webhook: OUTBOUND-ONLY** (it can post into a
group; it cannot receive replies, @mentions, or uploads — real inbound needs a *full Feishu
app with event subscriptions*, a separate build). So M6 does NOT claim Feishu inbound:
- **Questions go OUT over Feishu** (works today) — the operator sees them in the group.
- **Answers come back via CLI / a file** the scope phase reads (e.g. `devagent run` resumed
  with an answers file) — NOT a Feishu reply.
Feishu inbound (reply-driven clarification) is deferred to M7+/later. When no operator is
available, the scope phase records its assumptions and proceeds (honest-failure if a hard
ambiguity blocks it).

### 4.2 Flexible model (`schema.py`)

Today's `Spec` (frontend `pages`/`components`, closed `stack` Literal) becomes **one
artifact kind among many**.

```python
class ArtifactSpec(BaseModel):
    type: str                 # "frontend" | "backend" | … (open; gated against registry)
    stack: str                # "node-vite-react" | "node-express" | … (open; gated)
    detail: dict              # type-specific shape (frontend: pages/components;
                              #   backend: endpoints/models) — validated by the recipe
    acceptance_checks: list[AcceptanceCheck]

class ProjectScope(BaseModel):
    title: str
    targets: list[ArtifactSpec]      # ≥1; the fullstack proof has 2 (frontend + backend)
    repo: RepoBinding | None = None  # None = no repo (M7 fills this in for real)
```

`AcceptanceCheck.kind` gains **`api_json`** (HTTP request → assert JSON body shape against
the booted backend). `command_exit` / `stdout_matches` are added to the **dispatch seam**
now (cheap) so future CLI/MCP recipes need no schema change, even though no M6 recipe emits
them. Validators stay machine-checkable (the defining property of a spec).

`Plan` (disjoint file ownership) is unchanged — it already supports an arbitrary file set
across targets.

### 4.3 Recipe registry (`recipes/`, new — open/extensible)

A **recipe** is the reliable know-how for building one artifact type:

```python
@dataclass(frozen=True)
class Recipe:
    name: str                  # "node-express"
    type: str                  # "backend"
    toolchain: Toolchain       # the sandbox/image this recipe needs (provisioned per project)
    scaffold_hint: str         # what the build prompt tells the model to scaffold
    build_cmd: str             # "pnpm -r build"
    artifact_check: str        # proof a real build ran (glob/path)
    boot: BootSpec | None      # how to start it for acceptance (server+port+health) | None=static
    supported_checks: tuple    # AcceptanceCheck kinds valid for this type
```

`registry.py` maps `name → Recipe`. **M6 registers two:**

| Recipe | type | build | boot for acceptance | check kinds |
|---|---|---|---|---|
| `node-vite-react` | frontend | `pnpm build` → `dist/` | static serve | `route_status`, `selector_present` |
| `node-express` | backend | `pnpm build` (tsc) | `node dist/server.js` + wait `/health` | `api_json`, `route_status` |

**Backend framework: Express** — chosen for the most training-data coverage → best
first-pass quality (the research-Q1 reliability lever). The Scope phase still *selects* it
per request; it is not the only possible backend.

**M6 builds only types that have a registered recipe** — because verify rebuilds from source
using the recipe's `build_cmd`/`BootSpec`; a type with no recipe has no build/boot commands
to run, so it cannot be gated-built. The Scope phase may *classify* any request (so the
front end is genuinely open and an un-built type is recognized, not misread as "frontend"),
but an unreciped target yields a `ProjectScope` marked **not-buildable** and stops with an
honest "no recipe yet" — never a half-build. The "open / attempt-anyway" property is a
**future** registry capability (add a recipe → the type becomes buildable, no harness
change), not M6 runtime behavior.

### 4.4 Per-project toolchain (`Toolchain`)

The sandbox is **derived from the chosen recipes**, not hardwired. For the fullstack slice
both recipes are Node, so the project resolves to a single Node toolchain (today realized by
the existing node sandbox image) — *because the scope resolved to Node*, not because it is
fixed. A later non-Node recipe declares its own `Toolchain`; the executor provisions per
target. (When targets share a toolchain, one build container serves the whole workspace;
per-target containers are only needed for mixed toolchains — a concern that first arises in
M7's Java path.)

### 4.5 Build / verify / acceptance (generalize `executor_*`, `verifier.py`, `acceptance_runner.py`)

- **Layout:** a pnpm-workspace monorepo — `apps/web`, `services/api`, and
  `packages/shared` for the **shared API contract** (types the frontend and backend both
  import) so the targets genuinely work together.
- **Build:** the executor builds the workspace from the per-recipe prompts. `BuildResult`
  is unchanged; `success` remains a claim the gates do not trust. Both A/B arms inherit this.
- **Verify (`verifier.py`):** rebuild-from-source per target using each recipe's
  `build_cmd`, assert each `artifact_check`, then — per recipe `BootSpec` — boot the backend
  and wait-for-health (frontend served static; nothing booted for a static target). The
  `VerifyReport` aggregates per-target results; per-target failures fold into the repair
  loop's diagnostics (cap 2, unchanged).
- **Acceptance (`acceptance_runner.py`):** boot logic is driven by the recipe `BootSpec`,
  not hardcoded to static `dist/`. Dispatch gains `api_json` (and the plumbed
  `command_exit`/`stdout_matches`). The HTTP path stays browserless; Playwright stays lazy.

### 4.6 Deploy / preview (`deploy.py`, `phases/deploy.py`)

Per-target local preview: the backend runs as a container on a host port; the frontend is
served with its API base pointed at the backend, so the previewed app actually talks to its
API. Both URLs are reported. (Real GitLab/k8s deploy is M7.)

## 5. The fullstack E2E proof (the M6 acceptance bar)

A single live run from a fullstack PRD produces: a confirmed `ProjectScope` with two
targets (frontend `node-vite-react` + backend `node-express`), a built monorepo, a green
rebuild-from-source for **both** targets, the backend booting and passing `api_json`
checks, the frontend passing `route_status`/`selector_present`, a local preview where the
frontend reaches the backend, and a run report. Docker/`DEVAGENT_RUN_LIVE`-gated.

## 6. A/B (M5) implications

The Executor seam is untouched, so both arms inherit multi-target builds. M5 will run its
A/B on this fullstack workload. (The managed arm's ability to build a multi-target Node
workspace in its cloud sandbox is a live-probe to confirm during M5 prep, alongside the
already-tracked managed token/cost capture.)

## 7. Testing

- **Scope phase** — unit-tested with fixture requests across varied types (backend-only,
  MCP, "copy Airbnb", a Claude skill) to prove classification does **not** collapse to
  "frontend", that ambiguity yields sensible clarifying questions, and that answers are
  consumed into the scope.
- **Registry + flexible schema** — pure unit tests: open `type`/`stack` validation, the
  `ScopeGate`, the new `api_json` check kind, machine-checkability preserved.
- **Multi-target build/verify wiring** — a `FakeExecutor` + a fake recipe drive the full
  matrix path with no Docker/tokens.
- **Live fullstack build** — the §5 acceptance bar (docker/live-gated).

## 8. Risks / open items

- **Feishu inbound clarification** — keep minimal in M6 (reply-capture/CLI), do not build a
  full bot. Flagged so scope does not creep.
- **Managed arm multi-target** — confirm during M5 prep (live probe).
- **Novel-type attempts** — must fail honestly ("no recipe yet"), never silently "succeed"
  or half-build; covered by the no-recipe path test.
- **"Work together" is previewed, not gated.** `api_json` checks the backend directly and
  `selector_present` checks the frontend DOM; no M6 acceptance kind asserts the frontend
  actually fetched live data from the backend. The fullstack wiring is demonstrated at
  preview but not deterministically verified. A cross-target integration check kind (e.g.
  drive the UI → assert it renders backend data) is a later increment.

## 9. Module-level change summary

| Module | Change |
|---|---|
| `phases/scope.py` | **new** — scope classification + clarification loop → `ProjectScope` |
| `phase_gates.py` | **new** `ScopeGate`; generalize gates to multi-target |
| `schema.py` | `ProjectScope` / `ArtifactSpec` (open-typed) replace frontend-only `Spec`; `api_json` (+ plumbed `command_exit`/`stdout_matches`) check kinds |
| `recipes/` | **new** — `Recipe`, `registry.py`, `node-vite-react`, `node-express` |
| `executor_sdk.py`, `managed_executor.py`, `sdk_runner.py` | per-recipe, multi-target build prompts; recipe-driven artifact checks (drop hardcoded `dist/index.html`) |
| `verifier.py`, `acceptance_runner.py` | per-target rebuild + recipe-driven boot + `api_json` |
| `deploy.py`, `phases/deploy.py` | per-target preview; frontend wired to backend |
| `phases/intake.py`, `phases/spec.py`, `phases/plan.py` | re-shaped around `ProjectScope` (intake folds into Scope) |
| Feishu channel (`channels/`) | minimal inbound answer-capture for clarification |
