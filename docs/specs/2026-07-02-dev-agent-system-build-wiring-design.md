# dev-agent — System-Build Wiring (build-system command + live integration)

**Date:** 2026-07-02
**Status:** Design (brainstorm approved). Activates the deferred live seams of M15–M17.
**Milestone:** M20 (system-build wiring). Depends on M14–M17 (built). Defers M7 (git-binding) and whole-system deploy.

## 1. Goal

Make the long-horizon builder **actually runnable**: one command takes a PRD, has the Architect design the service DAG, **builds each service for real**, **brings them up together**, and runs the **cross-service E2E** — the litmus from the design spec (§9). Today M14–M17 are unit-tested units with the `run_node` seam filled only by test fakes; this milestone fills it for real and adds a CLI entry point.

Success: `devagent build-system <prd>` on a multi-service PRD (with an `ANTHROPIC_API_KEY` + Docker) produces built services under `runs/<id>/services/<name>/`, boots them together, runs the declared cross-service flow, and reports per-service + integration pass/fail — bounded by one shared budget.

## 2. Scope

**In:** the `build-system` command; a real `run_node` (approach A — reuse the existing per-service pipeline); multi-container bring-up (reuse verifier/deploy machinery) producing `{service → base_url}`; wiring `IntegrationRunner`/`IntegrationGate`; a system report; one shared global `Budget`.

**Out (deferred, unchanged):** M7 git-repo accretion (services land in the run dir, not a bound git repo); whole-system deploy/preview; checkpoint/resume (M18); mutable contracts (M19); `db_schema`/`auth_token`/`event` conformance.

**Non-goal:** live end-to-end verification in *this* dev environment — it has no `ANTHROPIC_API_KEY`. This milestone is built + **unit-tested with fakes**; the operator runs it live (their key + Docker).

## 3. Architecture

```
devagent build-system <prd>
   │
   ├─ Architect phase ──▶ SystemDesign ──[ArchitectGate]
   │
   ├─ TreeOrchestrator(run_node = SystemBuilder.run_node, budget = shared) 
   │     └─ per ServiceNode, in dependency order (siblings concurrent):
   │           run_node → real per-service build → NodeResult
   │     ──▶ SystemBuildResult (per-service pass/fail)
   │
   ├─ if all services built: SystemBringUp(design, run_dir)
   │     └─ start each built service on a per-run docker network,
   │        health-check, collect {service → base_url}          (reuse deploy/verifier)
   │
   ├─ IntegrationRunner(design.integration_checks, base_urls) ──▶ IntegrationReport
   │     ──[IntegrationGate]
   │
   ├─ teardown (finally): stop containers, remove network
   └─ write runs/<id>/system-report + per-service artifacts
```

Two new components behind clean seams (both injectable so the orchestration is unit-testable without Docker/tokens):

- **`SystemBuilder`** (`devagent/system_build.py`) — holds the shared config (budget, run dir, executor, egress network) and exposes `run_node(node, design) -> NodeResult` (approach A) and `bring_up(design) -> (base_urls, teardown)`.
- **`build_system(...)`** (same module) — the deterministic orchestration function above. Takes `run_node` and `bring_up` as parameters (defaulting to `SystemBuilder`'s), so tests pass fakes.

The **CLI** (`cli.py`) gains a `build-system` subcommand that constructs the real `SystemBuilder` and calls `build_system`.

## 4. `run_node` — approach A (reuse the existing pipeline per service)

For each `ServiceNode`:

1. Write `node.prd_slice` to `services/<name>/prd.md`.
2. Compute the node's consumed contracts: `consumed_by_target = {node.name: [c.spec for c in contracts_for_node(node, design)]}` (M16 — inject `.spec` dicts, not `Contract` objects).
3. Build a per-service pipeline via a **new factored helper** `build_pipeline_phases(input_path, out_dir, run_id, executor, verifier, consumed_by_target=None)` — extracted from today's `cli.main` (`cli.py:97–124`) so `run` and `build-system` share one assembly. The scope phase consumes `prd.md`; the build phase writes into `services/<name>/out` and receives `consumed_by_target` through `enrich_scope`.
4. Run that pipeline on its own `Orchestrator` **sharing the one global `Budget`** (so the whole system build is bounded by `$50` / token / time caps together).
5. Map the sub-run's terminal status → `NodeResult(node.id, SUCCEEDED|FAILED, detail)`.

Rationale: each service build is byte-identical to today's proven, reviewed single-app path; the Architect supplies decomposition + contracts + order. The per-service `scope` re-derives the concrete buildable spec (acceptance checks/detail) from the slice — one extra LLM call per service, acceptable since contracts are stack-agnostic. (Honoring the Architect's exact `stack` is a later refinement — approaches B/C in the brainstorm.)

**Failure semantics:** unchanged — `TreeOrchestrator` blocks a failed producer's dependents, lets independent services proceed, and returns a partial `SystemBuildResult`. `run_node` never raises (a sub-run crash → `FAILED` NodeResult).

## 5. Integration bring-up

`SystemBuilder.bring_up(design)` reuses the verifier/deploy pattern (research note `scratchpad/m17-research.md`):

1. `deploy.ensure_network("devagent-sys-<run_id>")` — a fresh per-run bridge network.
2. In `topo_order(design)`: start datastores via `deploy.start_service`, then backends/frontends via `deploy.start_target` (backends detached on a free host port → URL; frontends static → URL), each with `--network-alias <name>`, injecting cross-service conn env.
3. Health-check each (`deploy._probe`), collect `{service_id → base_url}`.
4. Return `(base_urls, teardown_fn)`. The caller runs `IntegrationRunner`/`IntegrationGate`, then always calls `teardown_fn` in a `finally` (stop containers + remove network), mirroring `BuildVerifier.verify`'s teardown.

If a service fails to come up, its entry is absent from `base_urls` → the E2E steps for it fail cleanly (M17 already handles a missing base_url as a failed step). Integration runs only if every service built (else the system report shows the build failure and skips E2E).

## 6. CLI

```
devagent build-system <prd>
```
Constructs the real `SystemBuilder` (executor from `Config` A/B seam, egress network, `BuildVerifier`, one shared `Budget(cfg.max_*)`), runs `build_system`, writes `runs/<id>/system-report.{json,html}` (per-service statuses + integration report), prints the run dir + verdict. Feishu wiring (a "build a system" trigger) is a trivial follow-up once the command exists — out of scope here.

## 7. Testing

- **Unit (zero Docker, zero tokens):** `build_system` orchestration with a **fake `run_node`** (returns scripted `NodeResult`s) and a **fake `bring_up`** (returns a scripted `{service→base_url}` + no-op teardown) — asserts: Architect gate wired, tree runs in order, integration runs only when all built, gate verdict, teardown always called (even on integration failure), shared-budget threading. `SystemBuilder.run_node` tested with a **fake pipeline factory** (asserts it writes the prd slice, injects `consumed_by_target`, shares the budget, maps status→NodeResult) — no real Orchestrator.
- **Refactor safety:** `build_pipeline_phases` extraction is covered by the existing `run` tests (behavior unchanged) plus a direct test.
- **Live (operator):** the real end-to-end on a multi-service PRD with a key + Docker — documented, not run here.

## 8. Deferred (documented, not gaps)

M7 git-repo accretion (land in a bound repo, not the run dir); whole-system deploy/preview URL; checkpoint/resume across a days-long tree (M18); honoring the Architect's exact stack in `run_node` (approaches B/C); richer conformance kinds.

## 9. Reconciliation

- Activates the **deferred live seams** the M14–M17 reviews and design explicitly flagged (real `run_node`, container bring-up supplying `base_urls`). No new logic in M15–M17 — this wires them.
- **Reuses** the existing pipeline (approach A), the executor A/B seam, `enrich_scope`/`consumed_by_target` (M16), `deploy`/verifier bring-up, `TreeOrchestrator` + `IntegrationRunner`/`IntegrationGate` (M15/M17) unchanged.
- The one existing-code change is a **refactor** (`build_pipeline_phases` extracted from `cli.main`), which `run` and `build-system` then share — a targeted improvement, not unrelated refactoring.
