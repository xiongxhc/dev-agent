# dev-agent

A headless, autonomous **web-app builder**: give it a PRD (or, later, a reference URL),
and it produces a built, deployed web app — unattended. A deterministic Python harness
drives bounded Claude calls (**LLM brain, deterministic hands**), with a deterministic
gate between every phase.

**Status: M2 nearly complete — the full loop is live-verified.** `devagent run --build <prd>`
runs `PRD → intake → spec → plan → build → rebuild-from-source verify → acceptance → repair`
in one command, gated at every phase. A live run built a Vite+React+Tailwind app and passed
both a `route_status` and a real-chromium `selector_present` check (~$0.24, ~36s, 0 repairs).
Only the **egress allowlist** remains for M2 (the build/verify containers still use the full
bridge network).

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
| **Build Executor** — writes files, runs the build, iterates (the A/B seam) | arm A: **Claude Agent SDK** · arm B: **Claude Managed Agents** | ◐ arm A wired (A) / M4 (B) |

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
| `phases/build.py` | `BuildPhase` — adapts an `Executor` into the pipeline; folds build tokens into the shared Budget; owns the **repair loop** (build → verify → repair, cap 2) | via Executor |
| `executor_sdk.py` | `SdkExecutor` — contained Agent-SDK build arm (own disposable container); a repair pass is fed the prior verify diagnostics | **Agent SDK** |
| `verifier.py` | `BuildVerifier` — rebuild from source (`--frozen-lockfile`) **+ acceptance checks**; the trusted re-check (no API key) | no |
| `acceptance_runner.py` | runs in-container: boots a static server on `dist/`, runs the spec's checks **kind-dispatched** (`route_status`=HTTP, `selector_present`=Playwright, lazy) | no |
| `phase_gates.py` | `BriefGate`/`SpecGate`/`PlanGate`/`BuildGate`/`VerifyGate` (the build gates re-check the produced repo; they ignore the executor's `success` claim) | no |
| `phases/noop.py` | M1 containment probe | no |

---

## Run

```bash
python -m devagent.cli run examples/hello.md            # brain only: intake -> spec -> plan
# -> "run-<id> succeeded  -> N tasks; artifacts in runs/<id>"
# writes runs/<id>/{intake,spec,plan}.json + an append-only ledger.jsonl

python -m devagent.cli run --build examples/hello.md     # + contained build (needs Docker)
# -> SdkExecutor builds -> BuildGate -> VerifyPhase rebuilds from source -> VerifyGate
# -> built app in runs/<id>/out/
```

`--build` is opt-in because it requires Docker (the M2 sandbox image) and spends build
tokens. Without it, the default run stops after `plan` (still spends brain tokens).

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
  - ✅ M2 image (node + `claude` CLI + Agent SDK + pnpm); `SdkExecutor` **live-verified** —
    contained Agent SDK built + `pnpm build`-compiled a Vite+React+Tailwind app from a
    Spec+Plan in ~28s / ~$0.21
  - ✅ wired `SdkExecutor` as a gated `BuildPhase` into the CLI (`run --build`): full
    `PRD → spec → plan → contained build` in one command; `BuildPhase` folds build tokens
    into the shared Budget; `BuildGate` re-checks the produced repo on disk (`dist/index.html`)
    and **does not trust** `BuildResult.success`. Unit-verified with a fake executor (no
    Docker/tokens); one gated live `--build` run still pending operator go.
  - ✅ **deterministic build re-verification** + **repair loop** (`BuildVerifier` + `VerifyGate`,
    loop in `BuildPhase`): rebuilds the executor's output FROM SOURCE in a clean container
    (`rm -rf dist && pnpm install --frozen-lockfile && pnpm build`) — `--frozen-lockfile` is
    the pinned-deps check; `rm -rf dist` ensures a real build produced the bundle. On failure
    the executor is re-invoked with the verify diagnostics (`VerifyReport.log_tail` → the
    runner's prompt), cap 2, each spending a shared-Budget retry. Needs no API key (runs no
    model). Unit-verified with fakes (mocked subprocess); live container run pending operator go.
  - ✅ **acceptance checks** (`acceptance_runner.py`, **kind-dispatched**): after a green
    rebuild, boots a static server on `dist/` and runs the spec's `acceptance_checks` —
    `route_status` over plain HTTP (no browser; this is also the seam a backend API would
    reuse), `selector_present` via Playwright/chromium (imported lazily, only when a selector
    check exists). Failures fold into `VerifyReport.log_tail` so the repair loop can fix them;
    `VerifyGate` now requires build-green **and** all checks pass. M2 image gains Playwright.
    HTTP path unit-tested against a real local server; the Playwright path is docker/live-gated.
  - ✅ **token accounting fixed** + **full loop live-verified**: `sdk_runner` now reads the
    terminal `ResultMessage`'s CUMULATIVE `usage` (input + cache-create + cache-read; the old
    code dropped cache tokens — ~7× under-count) and persists the raw breakdown. Probed the
    real SDK usage shape live; pure helper unit-tested against it. M2 image rebuilt with
    Playwright; a live `--build` run went green end-to-end.
  - ⬜ remaining (the last M2 item): **egress allowlist** — restrict the build/verify
    containers to api.anthropic.com + the npm registry (they currently use the full bridge
    network). Needs a design choice (out-of-sandbox proxy vs docker network rules).
- **M3** ⬜ — deploy → preview URL + run report
- **M4** ⬜ — `ManagedExecutor` (Managed Agents) behind the same seam
- **M5** ⬜ — eval corpus + the A/B test (the two empirical unknowns: quality, cost)
