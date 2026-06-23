# dev-agent

A headless, autonomous **web-app builder**: give it a PRD (or, later, a reference URL),
and it produces a built, deployed web app — unattended. A deterministic Python harness
drives bounded Claude calls (**LLM brain, deterministic hands**), with a deterministic
gate between every phase.

**Status: M2 in progress.** The shared harness + the `intake → spec → plan` brain
pipeline are built and live-verified. The build Executor (which writes and builds the
actual app) is the next increment.

---

## Two layers — don't conflate them

There are two completely separate "which Claude tech" questions here:

### 1. What we use to *develop* dev-agent → **Claude Code Agent Teams**
The repo's code is built and reviewed by spawning **managed teammates** (the experimental
Agent Teams feature) — parallel Claude Code sessions that review, fix, research, and
build on disjoint files. This is our **dev-time tool**. It is interactive-only and is
**not part of the product**.

### 2. What dev-agent *runs on* at runtime → depends on the phase
| Phase | Runtime tech | Status |
|---|---|---|
| **Brain** — intake / spec / plan (emit validated artifacts, no code execution) | **Anthropic Messages API** (`anthropic` pkg, forced tool-use → pydantic) | ✅ built |
| **Build Executor** — writes files, runs the build, iterates (the A/B seam) | arm A: **Claude Agent SDK** · arm B: **Claude Managed Agents** | ⬜ next (A) / M4 (B) |

So today the product uses **only the Messages API**. The **Agent SDK** and **Managed
Agents** appear only behind the swappable `Executor` seam at the build step — that's
where the planned A/B test lives. Neither is wired yet.

> Why the brain uses neither: intake/spec/plan are **shared** across both A/B arms and
> only emit artifacts — the Messages API is the simplest correct tool. The
> Agent-SDK-vs-Managed-Agents choice only matters where code is actually executed: the
> build Executor.

---

## Architecture

```
PRD/URL ─▶ intake ─▶ spec ─▶ plan ─▶ [ Executor ] ─▶ verify ─▶ deploy ─▶ report
           └──────── shared, gated ────────┘   │      └────── shared ──────┘
                                    ┌───────────┴───────────┐
                              SdkExecutor              ManagedExecutor
                          (Agent SDK, your              (Managed Agents,
                           Docker sandbox)              hosted/self-hosted)
   a deterministic gate (schema-valid? build 0? routes 200?) sits after every phase
```

- **Deterministic harness** (`orchestrator.py`) owns sequencing, gates, budgets, and
  stop conditions — control flow is code, never the model.
- **Executor seam** (`executor.py`) is the one swappable component; everything else is
  shared, which is what makes the A/B fair. `BuildResult.success` is the executor's
  claim and is **not trusted** — gates re-check the produced repo.
- **Hardened sandbox** (`sandbox.py`) — disposable `docker run --rm`, network-closed by
  default, all caps dropped, read-only rootfs, non-root, `out/` the only writable mount.
  Brain phases use a `NullSandbox` (host-side, no container).

Design + research: `../docs/superpowers/specs/2026-06-22-dev-agent-research-synthesis.md`.

---

## What's built (by module)

| Module | Job | Uses Claude? |
|---|---|---|
| `orchestrator.py` `budget.py` `ledger.py` `gates.py` | harness: phase loop, hard ceilings, audit ledger, deterministic gates | no |
| `sandbox.py` | hardened disposable Docker sandbox (+ `NullSandbox`) | no |
| `schema.py` | pydantic `Brief`/`Spec`/`Plan` (Spec checks are machine-checkable; Plan ownership is disjoint) | no |
| `executor.py` | the `Executor` Protocol + frozen `BuildRequest`/`BuildResult` seam | no |
| `llm.py` | `generate_structured()` — Messages API forced tool-use → validated pydantic | **Messages API** |
| `phases/intake|spec|plan.py` | the brain pipeline | **Messages API** |
| `phase_gates.py` | `BriefGate`/`SpecGate`/`PlanGate` | no |
| `phases/noop.py` | M1 containment probe | no |

---

## Run

```bash
python -m devagent.cli run examples/hello.md
# -> "run-<id> succeeded  -> N tasks; artifacts in runs/<id>"
# writes runs/<id>/{intake,spec,plan}.json + an append-only ledger.jsonl
```

Brain phases spend tokens (Messages API). The key is read from `ANTHROPIC_API_KEY` — put
it in a gitignored `dev-agent/.env` (`ANTHROPIC_API_KEY=...`) and `source` it, or export
it. Billing is **pay-per-token** — the Agent SDK / Managed Agents both require an API key
(Pro/Max subscription auth is not permitted for programmatic/headless use).

## Test

```bash
.venv/bin/python -m pytest -q                       # unit suite (no Docker, no tokens)
.venv/bin/python -m pytest -q -m docker             # containment + sandbox (needs Docker)
DEVAGENT_RUN_LIVE=1 .venv/bin/python -m pytest -q -m live   # live pipeline (spends tokens)
```
`DEVAGENT_REQUIRE_DOCKER=1` makes the containment suite **fail** rather than silently
skip when no Docker daemon is present (for CI).

---

## Milestones

- **M1** ✅ — skeleton + hardened disposable sandbox (proves containment, no tokens)
- **M2** ◐ — shared pipeline + `SdkExecutor`
  - ✅ brain phases (intake → spec → plan), gated, live-verified
  - ⬜ build Executor: M2 image (node + `claude` CLI + toolchain), `SdkExecutor` (Agent
    SDK fan-out in the sandbox), build gate (pnpm build + lint + pinned deps), verify
    phase (Playwright), repair loop (cap 2–3)
- **M3** ⬜ — deploy → preview URL + run report
- **M4** ⬜ — `ManagedExecutor` (Managed Agents) behind the same seam
- **M5** ⬜ — eval corpus + the A/B test (the two empirical unknowns: quality, cost)
