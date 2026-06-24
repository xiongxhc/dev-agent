# dev-agent

A headless, autonomous **web-app builder**: give it a PRD (or, later, a reference URL),
and it produces a built, deployed web app — unattended. A deterministic Python harness
drives bounded Claude calls (**LLM brain, deterministic hands**), with a deterministic
gate between every phase.

**Status: M2 complete — the full loop is live-verified, egress-contained.** `devagent run
--build <prd>` runs `PRD → intake → spec → plan → build → rebuild-from-source verify →
acceptance → repair` in one command, gated at every phase, with the build/verify containers
confined to an **egress allowlist** (api.anthropic.com + npm only). A live run built a
Vite+React+Tailwind app entirely through the proxy and passed both a `route_status` and a
real-chromium `selector_present` check (~$0.15, 0 repairs). It then **deploys** the app to a
local preview URL and writes an HTML **run report**. Next: M4 (the Managed-Agents A/B arm).

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
| `egress.py` `egress_proxy.py` | egress allowlist: an `--internal` network + a tiny CONNECT proxy (in the M2 image, no extra dependency) so build/verify reach only api.anthropic.com + npm | no |
| `deploy.py` `phases/deploy.py` `preview_server.py` | M3 deploy: a detached container serves `dist/` (SPA fallback) on a host port → preview URL; `DeployGate` proves it answers 200 | no |
| `report.py` | M3 run report: a self-contained `report.html` (phases, gates, tokens, cost, acceptance, preview URL) from the ledger | no |
| `phase_gates.py` | `BriefGate`/`SpecGate`/`PlanGate`/`BuildGate`/`VerifyGate` (the build gates re-check the produced repo; they ignore the executor's `success` claim) | no |
| `phases/noop.py` | M1 containment probe | no |

---

## Run

```bash
python -m devagent.cli run examples/hello.md            # brain only: intake -> spec -> plan
# -> "run-<id> succeeded  -> N tasks; artifacts in runs/<id>"
# writes runs/<id>/{intake,spec,plan}.json + an append-only ledger.jsonl

python -m devagent.cli run --build examples/hello.md     # + contained build (needs Docker)
# -> SdkExecutor builds -> rebuild-from-source verify + acceptance -> repair (cap 2)
# -> deploy to a local preview URL -> writes runs/<id>/report.html
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

## Deploy

The product runtime is the `devagent-sandbox:m2` image; the harness is just Python + a
Docker daemon + `ANTHROPIC_API_KEY`, so the whole loop runs anywhere Docker does. Containers
are disposable (`docker run --rm`) — nothing persists between runs except the built app under
`runs/<id>/out/`.

- **Build locally (this machine):** `sandbox/build.sh` → a native-arch image in your docker.
- **Build for deploy:** `REGISTRY=ghcr.io/<you>/devagent-sandbox sandbox/build.sh multiarch`
  → a `linux/amd64 + linux/arm64` image pushed to the registry. This Mac is **arm64**; most
  servers are **amd64**, so an arm64-only image silently won't run there — multi-arch is
  required (and, per a Docker limitation, can only output to a registry, so this mode pushes).

## Milestones

- **M1** ✅ — skeleton + hardened disposable sandbox (proves containment, no tokens)
- **M2** ✅ — shared pipeline + `SdkExecutor` + verify/acceptance/repair + egress
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
  - ✅ **egress allowlist** (`egress.py` + `egress_proxy.py`): build/verify containers run on
    an `--internal` Docker network (no direct route out) behind a tiny CONNECT proxy that runs
    in the M2 image (python3 only — no extra image to pull) and permits only api.anthropic.com
    + the npm registry. On by default (`DEVAGENT_EGRESS=0` to disable). Live-verified: a real
    `--build` ran end-to-end through the proxy; allow/deny/no-direct-egress codified as a
    docker-marked regression test.
- **M2 done.** Full loop live-verified end-to-end (build + rebuild-from-source + acceptance
  incl. Playwright + repair + egress + correct token accounting). Image: `sandbox/build.sh`.
- **M3** ✅ — deploy → preview URL (local SPA preview container) + HTML run report. Verified
  end-to-end (served a real build, gate got 200, report generated). Cloud static-host deploy
  is a later pluggable adapter.
- **M4** ⬜ — `ManagedExecutor` (Managed Agents) behind the same seam.
  - **DE-RISKED (2026-06-24):** Claude Managed Agents is GA-beta (launched 2026-04-08) — a
    hosted agent runtime via `/v1/agents` + `/v1/environments` + `/v1/sessions`, beta header
    `managed-agents-2026-04-01`, enabled by default, billed at token rates + **$0.08/session-hr**.
    The installed `anthropic` 0.111.0 SDK supports it: `beta.{agents,environments,sessions,files}`.
    Flow for the arm: create an agent (build prompt + `agent_toolset_20260401`) → cloud
    environment → session → stream `user.message`(Spec+Plan) to `session.status_idle` →
    `sessions.resources.list/retrieve` to pull the built app out of the **managed cloud sandbox**
    into `workdir/out` → delete session. Key A/B fact: arm B builds in **Anthropic's** sandbox
    (not our Docker), then the **shared** verify/acceptance re-checks the pulled output — the
    seam stays fair. No code/tokens spent yet; building + live-verifying is the next step.
- **M5** ⬜ — eval corpus + the A/B test (the two empirical unknowns: quality, cost)
