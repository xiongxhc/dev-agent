# dev-agent — Long-Horizon Multi-Service Builder (design / roadmap entry)

**Date:** 2026-07-02
**Status:** Design approved (brainstorm). Not yet planned. Depends on **M7**; composes with **M9**.
**Scope of this doc:** architecture-level vision for turning dev-agent from a single-app
builder into one that takes **one PRD**, *designs the services itself*, and builds a whole
system over a long-horizon (hours→days) autonomous run. Decomposed into ordered milestones
(**M14–M19**); the first milestone gets its own plan later.

---

## 1. Problem & goal

Today `devagent run --build <prd>` produces **one cohesive app**: `PRD → scope → plan →
multi-target build → verify → acceptance → repair → deploy`, in a single pass, bounded by
per-run ceilings (1M tokens, 30 min, $10). The live ceiling demonstrated at M6 was a
**2-target** fullstack monorepo (Express API + Vite/React), ~4 min, $0.13.

That is not enough to build a system like the **unified portal** (multi-service: MySQL,
auth, agent gallery, MCP servers, Dify integration — v1.0 alone was 26 plans / 7 phases).

Two things the current pipeline gets wrong for that job, both raised by the operator:

1. **"A PRD is just requirements — you design the services."** Correct. The Scope phase
   already does a *shallow* version (it decides "web + api"), but it's **one level deep,
   single-pass, and flat**. Designing a multi-service system's boundaries, data model,
   auth model, and dependency graph is real architecture work that has no home today.
2. **"The limit can be set much higher."** Also correct — and this reveals that **budget
   was never the real blocker.** Raising the caps lets a *flat* plan run longer, but a
   flat plan **degrades in coherence** long before it degrades on tokens. The fix is not a
   bigger cap on one giant build; it is **many small, coherent sub-builds under a durable
   architecture**, each with its own modest budget. A days-long run is just **N bounded
   sub-runs**, not one 3M-token monster.

**Goal:** feed one PRD → dev-agent designs a service DAG → builds each service as an
isolated sub-run that accretes into one persistent repo → verifies the services actually
talk → deploys — running autonomously for as long as it takes.

## 2. Non-goals

- **Not** a flat mega-plan with a bigger budget (§1.2 — the anti-pattern we explicitly reject).
- **Not** human-gated mid-run. The operator chose **autonomous with hard self-verification
  gates** now, and **fully autonomous, report-at-end** as the north star. No human approval
  gates between milestones. (This is a deliberate deviation from the general human-gated
  workflow preference, made explicit for this capability: trust comes from strong
  verification gates, not human checkpoints.)
- **Not** a rewrite of the build executor, gates, or recipe system — all of that is reused
  unchanged, per sub-run.

## 3. Core decisions

### 3.1 Autonomy: option 3 now → option 2 north star
- **Now (M14–M18):** autonomous; **hard verification gates** between milestones/services.
  Stop and surface only on a gate failure the run can't self-repair.
- **North star:** remove the final rails → fully autonomous, hand back the built system + report.

### 3.2 Architecture: recursive pipeline (A) as the spine → mutable contracts (C) as the north star
Three approaches were considered:

- **A — Recursive pipeline (chosen spine).** A new **Architect** phase emits one
  `SystemDesign` (service DAG + contracts). The orchestrator becomes a **tree**: each
  service node runs the *existing, proven* `scope→plan→build→verify` pipeline as an isolated
  sub-run, accreting into one repo. Contracts are **frozen** and passed read-only down the
  tree. Least new machinery; coherence comes from isolation + contracts.
- **B — Flat mega-plan + bigger budget.** Rejected (§1.2, §2).
- **C — Blackboard with mutable contracts (chosen north star).** The `SystemDesign` becomes
  **mutable/versioned**: a sub-build that discovers its needs diverge from a contract can
  *renegotiate* it, and the orchestrator re-plans affected nodes.

**C is a strict superset of A.** You reach C by making the frozen artifact versioned and
adding a re-plan scheduler — **nothing in A is thrown away.** And C is the honest end goal
because **brownfield (M9)** requires renegotiation: contracts *discovered* from an existing
repo are never frozen-correct on the first pass. So A's `SystemDesign` artifact and
orchestrator are shaped from day one so "frozen → mutable" is an **additive milestone, not a
rewrite** (see §5, the seam).

## 4. The two load-bearing pieces

Everything else is plumbing around these two.

### 4.1 The `SystemDesign` artifact
A new pydantic model in `schema.py`, sibling to `ProjectScope`/`Plan`. It is the **only**
thing the Architect emits and the **only** thing the tree consumes:

```python
class Contract(BaseModel):
    id: str
    kind: Literal["openapi", "db_schema", "auth_token", "event"]
    producer: str                 # service id that implements it
    spec: dict                    # the actual interface (OpenAPI doc, DDL, token claims)
    version: int = 1              # A: always 1 (frozen). M19/C: bumps on renegotiation.

class ServiceNode(BaseModel):
    id: str
    name: str                     # repo subdir: services/<name>/
    kind: str                     # maps to existing ArtifactSpec.type (frontend/backend/worker)
    stack: str                    # recipe name (existing recipe registry)
    prd_slice: str                # the Architect's slice of the PRD for THIS service
    depends_on: list[str] = []    # DAG edges (other service ids)
    provides: list[str] = []      # contract ids it implements
    consumes: list[str] = []      # contract ids it calls

class SystemDesign(BaseModel):
    title: str
    services: list[ServiceNode] = Field(..., min_length=1)
    contracts: list[Contract] = Field(default_factory=list)
    version: int = 1
    # validators: DAG acyclic; every consumed/provided contract id exists;
    #             every consumer's dependency actually provides what it consumes.
```

**The load-bearing move:** a `ServiceNode` + the `Contract`s it consumes + its `prd_slice`
is *exactly enough* to synthesize a `ProjectScope` for one service. That is what lets the
existing pipeline run unchanged, per node.

### 4.2 How a sub-run is spawned
The orchestrator stops being a flat `list[Phase]` (`orchestrator.py:38`) and becomes a
**topological scheduler over the DAG**. Per node, in dependency order (siblings with no edge
between them run concurrently — M12's parallelism lifted from files to services):

1. **Synthesize a scoped input:** `prd_slice` + the consumed `Contract`s rendered read-only
   into the build prompt ("you MUST call this API / conform to this token shape") + target
   subdir `services/<name>/` + a **per-node `Budget`** + the shared `ledger`.
2. **Run the existing pipeline** (`scope→plan→build→verify`) as a sub-run — the current
   `Orchestrator.run()` becomes the per-node executor, called recursively. **Zero new build
   machinery.**
3. **On green:** mark node done, record its produced contract artifacts, accrete output into
   the persistent repo (M7).
4. **On unrecoverable gate failure** (after repair budget): stop that subtree and surface —
   don't build consumers on a broken producer.

After the tree drains → **integration verify:** compose the built services, run the
cross-service E2E (the litmus).

**End-to-end data flow:**
`PRD → Architect → SystemDesign [gate: acyclic + contracts resolve] → tree scheduler → per
node {consumed contracts + prd_slice → existing scope→plan→build→verify → accrete to
services/<name>} [gate: node green + contract-conformant] → integration verify (compose +
E2E) → deploy → report`

## 5. Gates, failure semantics, budget & resume

**Gates** (deterministic, re-checked — per the existing principle that `BuildResult.success`
is the executor's claim and is not trusted):

- **`ArchitectGate`** — `SystemDesign` schema-valid, DAG acyclic, every consumed contract has
  a producer, every edge's producer actually `provides` what the consumer `consumes`.
- **Per-node gates** — the existing scope/plan/build/verify/acceptance gates run **unchanged**
  inside each sub-run.
- **`ContractConformanceGate`** — after a node builds, verify it conforms to the contracts it
  `provides` (e.g. the API actually serves the OpenAPI paths; the token has the declared
  claims). **This is the frozen→mutable seam:**
  - **A:** mismatch = **fail-stop** ("service X needs a field contract Y lacks" → stop, surface).
  - **C (M19):** the *same detection* emits a `ContractChangeRequest` → bumps `Contract.version`
    → re-queues dependent nodes. Same signal, different handler.
- **`IntegrationGate`** — all services up via compose, cross-service E2E passes (the litmus).

**Failure semantics (autonomy option 3):** a node that fails after its repair budget stops
that subtree; independent siblings continue; dependents are blocked; the run ends with a
**partial result + report** showing which nodes are green/red. Never build a consumer on a
broken producer.

**Budget:** per-node `Budget` (its own ceiling, keeps each sub-build MVP-sized) **plus** a
global run envelope (cost/time governance so a days-long tree can't run away). Raising caps is
now safe *because* each sub-build stays small — the cap protects the envelope, not a single
monster build.

**Resume (M18):** tree state (done nodes, their contract outputs, bound-repo SHA) is durable
in the run dir; a killed run resumes from the last green node. **Reuse the M5 eval harness's
resume pattern** — it is already resumable and degrades gracefully.

## 6. Repo layout & accretion (M7-dependent)

- **One bound repo (M7).** Each service builds into `services/<name>/` (monorepo). M7 is a
  **hard prerequisite** — today the sandbox is disposable `docker run --rm` with `out/` the
  only writable mount; a system that grows service-by-service over days needs durable
  accretion, which is exactly M7's git binding (`RepoBinding` already exists in `schema.py`).
- **Generated `docker-compose.yml`** at repo root — the Architect emits it from the DAG to
  wire services + shared datastore for integration verify. (The verifier already injects
  postgres/mongo/mock-IdP service containers per-run; integration verify generalizes this to
  the whole set at once.)
- **Contracts persisted** under `.devagent/contracts/` (versioned) for resume + audit.
- **`SystemDesign` + tree state** persisted in the run dir (browsable, like the Feishu bot's
  stable runs dir).

## 7. Testing strategy

Matches the repo's existing discipline (heavy unit coverage, zero-Docker/zero-token for
harness logic, live E2E for the real thing — the M5 harness has 249 such unit tests):

- **Unit (zero-Docker, zero-token):** `SystemDesign` validators (acyclic, contract
  resolution); topo scheduler ordering + sibling concurrency; sub-run spawning (mock the inner
  `Orchestrator`); the `ContractConformanceGate` detector; resume-from-checkpoint.
- **Small integration fixture:** a 2-service fixture (api + web) that must `compose up` and
  pass one cross-service E2E — the smallest instance of the litmus, runnable in CI-ish local.
- **Live litmus:** the 3–5 service demo from one PRD (§9).

## 8. Milestone decomposition

Ordered. New milestones are **M14–M19** (after the existing M13 team-rollout). They **compose
with** existing planned milestones rather than replacing them.

| # | Milestone | What it adds | Self-verify gate |
|---|-----------|--------------|------------------|
| **M7** *(prereq, planned)* | Git / persistent accretion | Sub-builds accrete into one durable repo, not ephemeral `out/` | Repo persists + builds clean across sub-runs |
| **M14** | **Architect phase** | PRD → `SystemDesign` (DAG + **frozen** contracts + data model + auth model), gated | `ArchitectGate`: schema-valid, acyclic, contracts resolve |
| **M15** | **Recursive orchestrator** | Flat `list[Phase]` → tree; each node runs existing pipeline as isolated sub-run; per-node budgets; dependency ordering + sibling concurrency | Each node builds green; tree completes in topo order |
| **M16** | **Contract injection + conformance** | Freeze OpenAPI/DB-schema/auth-token; inject read-only into each sub-build; `ContractConformanceGate` | Consumer builds against producer's real contract; producer conforms |
| **M17** | **Integration verification** *(litmus)* | Generated compose; bring services up together; cross-service E2E gate | 3–5 services boot via compose, E2E flow passes |
| **M18** | **Checkpoint / resume + cost governance** | Durable tree state; resume days-long run after crash; global run envelope | Kill mid-tree → resume → completes within envelope |
| **M19** | **Mutable contracts (→ C)** | Versioned `SystemDesign`; sub-build renegotiates; orchestrator re-plans affected nodes | Induced contract change → dependents re-plan + pass |
| **M9** *(planned, composes)* | **Brownfield** | Architect maps an existing repo first, designs additions that fit — **leans on M19's renegotiation** | Extends a real repo; existing tests still pass |
| ★ | **North star** | Remove final rails → fully autonomous, report-at-end | Whole system from one PRD, unattended |

**M17 is the "capability works" milestone (the litmus). M19 + M9 are the reach toward C and
the real portal use case.**

## 9. Success litmus

From one PRD, dev-agent designs and builds a **3–5 service app** (e.g. `web` + `api` +
`worker` + `auth` + `db`), brings them up together via the generated compose, and a
**cross-service E2E flow passes** (frontend → real API → real DB → auth). Provable, bounded,
and the natural next step above the M6 2-target live run.

## 10. Reconciliation & relationship to existing roadmap

- **`no-tier-phase-ceremony`:** this does not violate that rule. That rule forbids splitting
  *one capability* into fake v1/v2 tiers. This is a **dependency-ordered decomposition of
  many real services** and a genuine capability ladder — architecture, not ceremony.
- **`devagent-research-spec-when-unclear`:** the Architect phase *is* the research+spec step
  for the unknown (it designs services the PRD doesn't name), so the runtime never stalls for
  a human.
- **Composes with** M7 (hard prereq: durable repo), **M8** (per-project CI/deploy applies per
  service), **M9** (brownfield reuses M19's contract renegotiation), and is orthogonal to
  **M13** (team rollout / concurrency), though a days-long tree strengthens the case for the
  managed executor arm.
- **Reuses unchanged:** the executor seam (SDK/managed), recipe registry (M11), auth depth
  (M10), parallel-build primitive (M12, lifted to service granularity), and the M5 eval
  harness's resume pattern.

## 11. Open questions (for the plan phase, not blocking this design)

- **Where a days-long run executes.** SDK arm (local Docker) vs managed arm — the executor
  seam already abstracts this; managed likely fits multi-day better (no laptop babysitting).
  Decide per M5's A/B outcome + M13.
- **Contract inference fidelity.** How rich the frozen OpenAPI/DDL contracts need to be to
  keep consumer builds honest before M19 renegotiation exists. Start minimal (paths + shapes),
  measure at M17.
- **Compose generation vs recipe.** Whether the root `docker-compose.yml` is architect-emitted
  or a recipe-registry template. Lean recipe if one generalizes.
