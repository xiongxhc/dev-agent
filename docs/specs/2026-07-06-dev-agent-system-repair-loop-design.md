# Dev-Agent: System-Level Repair Loop — Design

**Date:** 2026-07-06
**Status:** Draft — design approved in session
**Scope:** `devagent/system_build.py` (the loop) + the seams it needs: `run_node` gains an
optional `repair_context` (threaded through `build_service` → `_real_build_service` →
`build_pipeline_phases` into `BuildPhase`'s initial `BuildRequest`), and the repair sub-run
reloads the persisted plan without a fresh LLM `PlanPhase` — a new `FrozenPlanPhase` (mirroring
`FrozenScopePhase`) selected by a `plan=` argument on `build_pipeline_phases`. The in-service
repair loop inside `phases/build.py` (`BuildPhase._build`) is otherwise untouched.

## Problem

Repair already exists at the service level: `BuildPhase` re-invokes the executor with verify
diagnostics up to `max_repairs=2`, budget-capped (`phases/build.py:65`). At the **system**
level there is none: `build_system` tears down every container on integration failure and
ends the run `integration_failed` (`system_build.py:332`). Since one-flow moved check
derivation onto frozen contracts, the in-service failure class is largely solved — failures
now concentrate exactly where no repair exists. Live-run evidence (2026-07-03): an acceptance
failure was detected, no repair was attempted, and the run sat until a human diagnosed it by
hand.

## Decision summary

| Question | Decision |
|---|---|
| Where the loop lives | Inside `build_system`, wrapped around the bring-up + integration block — mirroring the existing rule "repair lives in the build phase, not the orchestrator": system repair lives in the system-build function, not a new layer |
| Failure attribution | **Deterministic, no LLM.** Each failing `IntegrationReport` step names its service (`integration.py:54`); implicated nodes = those services, filtered to build-kind (datastore/service-kind nodes have no buildable artifact). A `no base_url for service` step implicates that node too — it built but never became healthy in system context |
| Trigger seam | Attribution consumes **failing steps** (`{service, route, detail}`) — not `IntegrationReport` the type. The integration report supplies these natively; any later gating verifier (M24's security phase) feeds the same loop by rendering its findings as failing steps. The loop never learns a second input type |
| Repair pass | Re-invoke the executor on the node's **existing** `out/` repo with `repair_context` = the full integration report (all steps, its own marked) + its frozen contracts. `BuildRequest.repair_context` already exists (`executor.py:28`); it reaches the executor because `run_node` — the only build seam `build_system` holds — gains an optional `repair_context` it threads down through `build_service`/`_real_build_service`/`build_pipeline_phases`. The loop cannot call `build_service` directly (`build_system` never sees it; it is bound inside `make_run_node`) |
| No re-planning | The repair sub-run reloads the frozen scope (`FrozenScopePhase` + `scope_for_node`) and the persisted plan (`out/.devagent/plan.json`, written by the executor) via a new `FrozenPlanPhase` selected by a `plan=` argument on `build_pipeline_phases` — build+verify only. Without that seam, `build_pipeline_phases` always appends `PlanPhase()` (an LLM call) with no way to inject a plan, so "reload the plan, don't re-plan" is not expressible. No new plan/scope LLM call: a re-plan could restructure what integration already half-validated |
| Re-verification | Full: teardown, fresh bring-up, then a single `reverify(design, base_urls)` step that re-runs the entire derived integration suite AND (once M24 lands) the security verify phase, merging both into one `report`/`ok` verdict — never just the failed steps. The integration `runner` alone cannot do this: it executes `IntegrationChecks`, not an LLM-triaged probe with a second principal, so re-verify is this combined step, not a bare `runner(...)` call. A security-triggered repair must re-pass functional checks and vice versa |
| Caps | `max_system_repairs` (config, default **1**) is the loop's **own** counter — NOT `budget.spend_retry()`. Each repair pass re-enters `BuildPhase`, whose in-container loop already spends shared-Budget retries (up to `max_repairs=2`) and accrues real tokens/dollars; the existing token and `max_cost_usd` ceilings abort doomed loops exactly as they do in-build. The system loop must not *also* draw the shared retry pool: doing so double-counts (one system pass = up to 3 shared retries) and lets in-service repairs exhaust `max_retries` before the loop ever runs, silently defeating `max_system_repairs` |
| On exhaustion | `integration_failed` as today, but `SystemReport` now records what repair attempted (implicated nodes + per-pass outcome) so a failed run is diagnosable |
| Out of scope | `build_failed` nodes (they already exhausted their in-container repairs; a system-level retry would repeat the same loop) · stall/hang detection (external observation — the team-lead agent, see `2026-07-06-team-lead-agent-design.md`) |

## Flow

```
build_system:
  architect ─→ per-service builds (each with its own in-container repair loop, unchanged)
     ─→ bring_up ─→ integration
           ok   ─→ keep running as preview (unchanged)
           fail ─→ [NEW] attribute failing steps → repair each implicated node
                        (executor on existing out/, repair_context = integration report)
                   → teardown → bring_up → full re-verify
                        (integration suite + security phase once M24 lands)
                        ok   ─→ preview
                        fail / repairs exhausted ─→ integration_failed
                                                    (report carries repair attempts)
```

## Components

### 1. Attribution (pure function)

`implicated_nodes(steps, design) -> list[node]`: failing steps → service ids → design nodes,
excluding nodes whose recipe kind is `service`. Deduplicated, in `topo_order`. If every
failing step maps to a service-kind node (nothing repairable), skip repair and fail as today.

The input is the list of failing steps (`{service, route, detail}`), not `IntegrationReport`
itself — that keeps the seam open for M24: its security phase gates by rendering each gating
`Finding` as one failing step (`{service, route, detail: evidence+remediation}`), so security
findings enter this exact loop with no new code path (see the M24 design).

### 2. Repair pass (seam change)

The seam chain from what `build_system` actually holds down to the executor:
`run_node(node, design, repair_context=None)` (the injected callable) →
`build_service(node, design, svc_dir, budget, ledger, repair_context=None)` →
`_real_build_service`, which threads it through `build_pipeline_phases` (also gaining a `plan=`
argument, see the No-re-planning row) into `BuildPhase`'s **initial** `BuildRequest`. In repair
mode the sub-run pipeline is `FrozenScopePhase` + `FrozenPlanPhase` + build(+verify) only, on
the existing `svc_dir/out`.

Note on the in-build loop (`phases/build.py`, untouched): only the executor's **first**
invocation in the repair sub-run carries the integration report as `repair_context`. If that
node's local verify then fails, the existing in-build loop re-invokes with the verify
`log_tail` (`replace(req, repair_context=report.log_tail)`) — it **overwrites**, it does not
chain. That is acceptable: the integration report seeds the fix, and the full combined
re-verify after bring-up is the real oracle for whether the cross-service bug is gone. We do
not claim the two contexts are concatenated, because the untouched code does not do that.

### 3. The loop (in `build_system`)

The loop **replaces** the current `if not ok: teardown(); return integration_failed` block —
it is woven into `build_system`'s existing structure, not bolted before it, because the success
emission (`system_deploy` ledger + `SystemReport(..., "succeeded", urls=base_urls)`) and the
failure emission must both read the loop's *final* `ok`/`base_urls`/`report`, not the pre-loop
ones:

```python
ok, report, base_urls, teardown = <first integration pass, as today>
try:
    for attempt in range(max_system_repairs):
        if ok: break
        nodes = implicated_nodes(failing_steps(report), design)
        if not nodes: break
        teardown()                                 # tear down the failed stack before rebuild
        for node in nodes:
            run_node(node, design, repair_context=render(report, node))  # existing out/, no re-plan
        base_urls, teardown = bring_up(design)
        report, ok = reverify(design, base_urls)   # integration suite + security phase (M24),
                                                   # merged into one report/verdict
    if not ok:
        teardown(); return _finish(SystemReport(..., "integration_failed", ...))
except Exception:
    teardown(); raise                              # a mid-pass bring_up/reverify crash still tears down

# success path keys on the loop's FINAL base_urls/report — never the stale pre-loop values
ledger.append({"event": "system_deploy", "urls": dict(base_urls)})
return _finish(SystemReport(..., "succeeded", urls=dict(base_urls)))
```

Two invariants the pre-loop code did not need: **(a)** `ok` is recomputed each pass and the
post-loop success/failure branch keys on the loop's final `ok` — a repaired-healthy system must
not fall through the stale pre-loop `ok=False` (which would tear down a healthy preview and
report `integration_failed`), and the emitted `urls` must be the loop's rebound `base_urls`, not
the first bring-up's. **(b)** `teardown()` runs before **every** `integration_failed` return —
repairs exhausted, `not nodes`, or a `bring_up`/`reverify` raise (the `try/except` guard) —
matching today's guaranteed teardown-on-failure; otherwise a failed repair leaks the entire
second preview stack (containers with `--restart unless-stopped`, volumes, per-run network).

Ledger events: `system_repair_start` (attempt, implicated node ids) and `system_repair_end`
(per-node status) — without these a repaired run's ledger reads identically to a clean one.

### 4. Report

`SystemReport` gains `repairs: list` (one entry per pass: implicated nodes, per-node build
status, post-repair integration ok) and, once M24 lands, the gating security `findings` that
drove any security-triggered repair. Status vocabulary **unchanged**
(`design_failed | build_failed | integration_failed | succeeded`): a security-gated exhaustion
terminates `integration_failed` — the generic "system-level gating verification failed" bucket
— with the findings recorded so the operator sees the cause was security, not functional
integration. M24 must NOT add a fifth `security_failed` status: the four-value set is what
`SystemReport` readers and team-lead triage are built against.

## Known edge: wrong attribution

A failing frontend step whose true cause is the API. Mitigations: (a) the repair executor
receives the **whole** report, not just its own steps, so the transcript shows the cross-
service picture; (b) the re-run of the full integration suite is the oracle — a mis-repair
fails again and the run ends with two data points instead of zero. The cap prevents
ping-pong repairs.

## Testing

`run_node` (now carrying `repair_context`), `bring_up`, and `integration_runner` are
injectable, so the loop is unit-testable without Docker or tokens:

- integration fails once → `run_node` re-invoked with the report as `repair_context`, teardown
  before re-bring-up, combined re-verify run, `SystemReport.repairs` populated, ledger events
  present; on repair success the report is `succeeded` with the **loop's** `base_urls` in `urls`
  and a `system_deploy` ledger event (guards the stale-`ok` handoff bug).
- integration fails twice with `max_system_repairs=1` → `integration_failed`, one repair
  recorded, **and teardown ran** (no leaked containers on the exhausted exit).
- failing steps all map to service-kind nodes → no repair attempted, teardown ran.
- a `bring_up`/`reverify` raise mid-pass → run ends by re-raising with teardown having run (no
  leaked stack).
- the shared retry pool is exhausted by in-service repairs → the system loop still runs its
  `max_system_repairs` passes (it does not draw `spend_retry`), bounded only by its own counter
  and the token/$/time ceilings.
- a non-integration trigger — fixture security findings rendered as failing steps — drives the
  same attribution → repair → combined re-verify path (guards the M24 seam).

Live validation: system-polls example with one service's contract check doctored to fail
first pass; confirm the repair pass fixes and the preview comes up.
