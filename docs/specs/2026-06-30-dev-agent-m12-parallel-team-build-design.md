# M12 — Parallel / team build

**Status:** built + unit-verified (2026-06-30, hardened after code review). Concurrent
independent-target build shipped in `SdkExecutor` (one contained SDK session per `kind=="build"`
target, capped at `min(targets, DEVAGENT_BUILD_CONCURRENCY|3)`, aggregated into one `BuildResult`).
Parallelism is **gated on a clean plan partition** — every build target owns ≥1 task and every task
belongs to exactly one target; a non-partitionable plan (shared/root task, cross-target task, empty
target), a **repair pass**, and single-target scopes all fall back to the proven sequential single
session, so the disjoint-`/out` invariant is guaranteed rather than assumed. The intra-target
lead-agent + subagents path (a single large coupled target fanning out) remains a follow-up — it
needs the in-container SDK's Task tool and a live run to verify.
**Goal:** build the app with **multiple agents working concurrently** instead of one agent doing
every task sequentially — wall-clock ≈ the slowest slice, not the sum.

---

## The seam is already there

dev-agent specs and plans before any code: `scope` (the spec) → `plan` (the task breakdown) →
`build`. And `PlanGate` already enforces **pairwise-disjoint file ownership** across tasks — the
schema comment says it outright: *"so parallel build agents never collide."* The plan is built for
this. What's missing is an executor that uses it: today `SdkExecutor` runs a **single** Agent SDK
session that implements everything sequentially.

## How it builds in parallel

The partition comes from the plan; the harness reads it and picks the build shape by the structure
of the work — no model improvising the strategy at runtime:

- **Independent targets run concurrently.** A fullstack scope has separate target dirs (`web/`,
  `api/`) with their own `node_modules` and `pnpm build`. `SdkExecutor.build()` runs **one
  contained SDK session per build-target** at once (each its own disposable container, dir, and
  install+build), then aggregates into one `BuildResult`. Collision-free by construction; isolated
  containers give real concurrent build environments. Services (datastores) have no build, skipped.
- **A large, coupled target uses a lead agent + subagents.** Inside a single target the work is
  coupled (shared types/config, one build at the end) and the split is non-obvious — so a lead
  agent coordinates subagents on disjoint source files with shared context, then one install+build.
  This is the agent-team pattern, used where shared-context coordination actually pays off.

Match the tool to the split: obvious + independent → code partitions, isolated containers run
concurrently; non-obvious + coupled → a lead agent coordinates subagents. Either way the partition
is decided from the plan (a gated artifact), not improvised live, and the **outcome is gated
deterministically** — verify rebuilds-from-source + re-runs acceptance no matter how it was built.

## What changes

**The Executor seam is unchanged** — `build(BuildRequest{full scope+plan}) -> BuildResult` keeps
its signature; parallelism is internal to `SdkExecutor`. So `BuildPhase`, the gates, verify,
acceptance, deploy, and the repair loop are untouched.

- **Partition** — split `scope.targets` into `kind=="build"` targets; map each to its plan slice
  (tasks whose `owned_files` sit under that target's dir).
- **Concurrent build** — launch one contained SDK session per build-target on a worker pool (cap
  concurrency at `min(n_targets, host_cap)`, env-overridable); each gets a target-scoped prompt
  (its tasks + recipe hint); all mount the shared `/out` (writes disjoint by dir). A large target
  may itself fan out to subagents on its disjoint files.
- **Aggregate** — combine into one `BuildResult`: `success = all`, tokens/cost **summed**,
  `wall_clock_s` = **max**, error = first failure. Fold into the shared `Budget` once after the
  join (no concurrent `Budget` mutation).
- **Repair** — `BuildPhase`'s existing loop still works (re-invoke the executor). Cheaper option
  later: re-run only the failed target's session.
- **Tests** — fake executor with 2 targets: concurrent dispatch, aggregated tokens/cost/`success`,
  single-target path unchanged. No Docker/tokens.

## Risks

- **Host pressure** — each target = an `m2` container (+ chromium). Cap concurrency (default
  `min(targets, 3)`, env-overridable).
- **Budget thread-safety** — aggregate spend after the join, not via concurrent `add_*`.
- **Cross-target contract** — `web` needs `api`'s URL, but that's runtime-discovered via
  `/config.json` at deploy, not a build-time code dependency between slices. The shape each slice
  must honor is in the scope/acceptance, not in a sibling's code.
- **Egress** — concurrent containers share the allowlist proxy; it already handles concurrency.

## Done when

- A fullstack (`web`+`api`) build runs both targets concurrently: wall-clock ≈ `max(target)`, not
  the sum; aggregated tokens/cost correct; both verify green; deploy unchanged.
- Single-target builds take the same path as today (no regression).
- Concurrency capped and configurable.
- A/B unaffected on what it measures (quality, cost) — parallelism is Executor-internal, only
  wall-clock moves. The managed arm can gain the same per-target partition the same way.
