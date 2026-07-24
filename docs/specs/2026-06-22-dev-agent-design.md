# DevAgent — Headless Autonomous Web-App Builder (v1 Design)

**Date:** 2026-06-22
**Status:** Draft — superseded on the backend question by
[`2026-06-22-dev-agent-research-synthesis.md`](./2026-06-22-dev-agent-research-synthesis.md),
which pivots to **build both backends behind a shared `Executor` seam and A/B test**.
This doc's architecture (deterministic harness, gated phases, sandbox, verification)
stands; read the synthesis for the build-both revision and hardening corrections.
**Author:** Chris Xiong (with Claude / Opus 4.8)

---

## 1. Goal & non-goals

**Goal.** A headless, unattended daemon/CLI that takes a requirement (PRD text, a
one-line ask, or a reference website URL) and autonomously produces a **built,
deployed web app reachable at a live preview URL**, plus a screenshot and a smoke-test
report — with no human in the loop during the run.

**v1 target (scoped).** Web apps only → preview URL. Frontend or simple full-stack
(static / Next.js / Vite). This is the visible, verifiable first vertical.

**Non-goals (v1).**
- "Build *anything*" (CLIs, mobile, data pipelines) — later verticals, same harness.
- Multi-service / k8s / production-grade infra — preview deploy only.
- Local-only (Ollama) brain — v1 is Claude via Agent SDK (see §7; hybrid is v2).
- Human-in-the-loop gating — v1 is fire-and-forget by design.

**Success criteria.** Given a PRD fixture, a single `devagent run prd.md` produces,
unattended: a git repo, a green build, a deployed preview URL that returns 200, a
screenshot, and a JSON run-report — or a clean, explained failure with the partial
artifacts preserved. Reproducible from a fixture corpus.

---

## 2. Architecture — deterministic harness, Claude-powered phases

The same pattern already proven in `trading-agent`: **LLM brain, deterministic hands.**

A thin **deterministic Python orchestrator** owns the state machine, the sandbox, the
budgets, and the stop conditions. Each phase is a **bounded Claude Agent SDK call**.
Only the **build** phase fans out into parallel **SDK subagents**. Between every phase
sits a **deterministic gate** that must pass before the next phase runs.

```
                ┌──────────────── deterministic orchestrator (Python) ────────────────┐
  PRD / URL ──▶ │ intake ─▶ spec ─▶ plan ─▶ build ─▶ verify ─▶ deploy ─▶ report       │ ──▶ URL + report
                │   │g       │g      │g    ╱│g╲      │g        │g                       │
                └───┼────────┼───────┼────┼─┼─┼──────┼─────────┼───────────────────────┘
                    │        │       │    │ │ │      │         │
                    └ each phase = one bounded Claude Agent SDK query() ┘
                                          │ │ │
                              build fans out to SDK subagents
                              (pages / api / styling / tests),
                              orchestrator synthesizes results
   gN = deterministic gate (schema valid? compiles? tests pass? 200 OK?) — hard stop if red
```

**Why this and not "one big autonomous agent":** control flow, retries, and
stop-conditions are *code you own*, not prompt-enforced hope. SDK subagents are
hierarchical (report-to-lead, no peer messaging), so coordination *must* live in the
harness anyway — the architecture isn't optional, it's the shape the SDK forces.

**On backends (revised — see synthesis):** Claude Code **Agent Teams** is
interactive-terminal-only and cannot run headless, so it is *not* a runtime candidate
(it's only a dev-time tool). But there are **two** viable headless backends, not one:
the **Agent SDK** (self-built loop+sandbox) and **Claude Managed Agents** (hosted REST;
cloud or self-hosted sandbox). The revised plan builds both behind a swappable
`Executor` seam and A/B-tests them; this section's single-backend framing is superseded.

### Module boundaries

| Module | One job | Depends on |
|---|---|---|
| `cli.py` | Parse `devagent run <input>`, load config, start a run | config |
| `orchestrator.py` | The phase state machine: sequence, gates, retries, budget, resume | phases, sandbox, ledger |
| `phases/*.py` | One file per phase; each wraps a single Agent SDK `query()` | sdk client, prompts |
| `sandbox.py` | Provision/destroy the disposable container; exec inside it | docker |
| `gates.py` | Deterministic pass/fail checks between phases | sandbox |
| `budget.py` | Token + wall-clock + retry accounting; raises on breach | — |
| `deploy/*.py` | One adapter per deploy target (vercel, tunnel) | sandbox |
| `report.py` | Assemble the JSON/markdown run-report + screenshot | — |
| `ledger.py` | Append-only run state on disk → enables resume + audit | — |

This mirrors your repo's existing split (`core/` engine vs `adapters/` per-target):
`deploy/` adapters and future per-vertical phase variants are the extension points.

---

## 3. The pipeline phases

Each phase: **input → bounded Claude SDK call → output artifact → gate**. Phases are
pure w.r.t. the ledger (resumable). All file work happens **inside the container**.

1. **intake** — Normalize the input into a structured `Brief`. If a reference URL:
   fetch + screenshot it, extract layout/sections/copy as structured notes (no
   pixel-cloning; capture intent). Gate: `Brief` is schema-valid and non-empty.
2. **spec** — Claude turns the `Brief` into a `Spec` (pages, components, data model,
   routes, acceptance checks). Gate: schema-valid; every acceptance check is
   machine-checkable (a URL path, a selector, a status code).
3. **plan** — Claude decomposes the `Spec` into an ordered task list with explicit
   file ownership per task (so subagents don't collide — the lesson from the
   trading-agent fix team). Gate: tasks cover all spec items; file ownership disjoint.
4. **build** — Fan out: one **SDK subagent per task-group** (e.g. pages, api,
   styling, tests), each owning disjoint files, each told to write code + its own
   tests. Orchestrator collects, writes to the repo, runs install. Gate: `build`
   command exits 0; lint passes.
5. **verify** — Run the suite + the spec's acceptance checks headlessly (the app
   boots, key routes 200, key selectors present). On failure → bounded **repair
   loop**: feed failures back to a repair subagent, re-verify, ≤ N attempts. Gate:
   all acceptance checks green OR repair budget exhausted (→ fail with diagnostics).
6. **deploy** — A `deploy/` adapter ships the verified build to the preview target,
   captures the URL. Gate: URL returns 200; post-deploy smoke check passes.
7. **report** — Emit `run-report.json` + markdown: URL, screenshot, spec coverage,
   token/$ spent, per-phase timing, what was cut, any residual warnings.

**Failure is a first-class output.** Any red gate that can't be repaired stops the run,
preserves all partial artifacts in the ledger, and writes an honest failure report —
never a false "done."

---

## 4. Sandbox & safety model

The daemon self-approves every command, so containment — not trust — is the control.

- **Disposable container per run.** `docker run --rm` a purpose-built image
  (Node + the build toolchain + the SDK runner). All codegen, install, build, and
  test happen inside. Destroyed at run end. Blast radius = the container.
- **No host reach.** No host filesystem mounts beyond a single `out/` artifact dir;
  no host SSH agent; no host env. The container cannot see your repos, secrets, or
  other projects.
- **Scoped, minted-per-run credentials.** Deploy tokens injected as env vars scoped
  to the preview target only (e.g. a Vercel token limited to a throwaway project),
  never your personal cloud creds. `ANTHROPIC_API_KEY` for the brain is the only
  broad secret, and it can only spend tokens, not touch infra.
- **Hard kill conditions** (enforced by `budget.py` / `orchestrator.py`, not prompts):
  - token budget per phase **and** per run (hard ceiling → abort);
  - wall-clock timeout per phase and per run;
  - max repair attempts per phase;
  - egress allowlist on the container (package registries + api.anthropic.com +
    the deploy target; deny the rest) to blunt exfiltration / surprise calls.
- **Auditability.** Every SDK call, command, and gate result appended to the ledger.
  A run is fully reconstructable after the fact.

v2 escalation path (when trusted): swap the container for a **dedicated cloud account**
with a spend cap and scoped IAM for real (non-preview) deploys.

---

## 5. Verification gates (the deterministic spine)

Gates are plain code, never the model's self-assessment. Minimum v1 set:

| After phase | Gate (deterministic) |
|---|---|
| intake | `Brief` validates against schema; required fields present |
| spec | `Spec` validates; ≥1 machine-checkable acceptance check; checks reference real routes/selectors |
| plan | tasks cover every spec item; file-ownership sets are pairwise disjoint |
| build | `npm/pnpm build` exit 0; linter exit 0; no missing-dependency errors |
| verify | test suite green **and** every acceptance check passes (boot app, hit routes, assert selectors/status) |
| deploy | preview URL returns 200; smoke check (title present, no console-fatal) passes |

A gate failure that the bounded repair loop can't clear = run failure. No gate is
"warn and continue."

---

## 6. v1 scope cut

**In:** single `devagent run <prd|url>`; web app vertical; disposable-container
sandbox; Claude-via-API brain; intake→…→report pipeline; deterministic gates; one
deploy adapter (start with a tunnel over `docker compose up` for zero external
accounts, then add a Vercel adapter); JSON+md report with screenshot; resume-from-ledger.

**Out (later):** other verticals (CLI/API/service); hybrid local-first brain;
multi-service infra; human-gated mode; dedicated-cloud-account sandbox; a queue/web UI
for submitting runs.

---

## 7. Cost model

Every run is **pay-per-token (API)** — subscription auth is prohibited for the SDK.
So cost is a design constraint, not an afterthought:

- Per-phase and per-run **token budgets** are hard ceilings in `budget.py`.
- Bounded phases (one `query()` with a turn cap) beat one open-ended loop — the failure
  mode of "agent spins for 200 turns" is structurally prevented.
- **v2 hybrid brain:** route cheap mechanical phases (scaffold, lint-fix, repair
  retries) to local Ollama, spend Claude tokens only on spec/plan/build reasoning.
  Deferred to keep v1 simple, but the phase boundary is where the router will slot in.

---

## 8. Key technical choices

- **Language:** Python (matches the repo; Agent SDK has a Python client).
- **Brain:** Claude via Agent SDK, `ANTHROPIC_API_KEY`, `query()` per phase,
  subagents for build fan-out.
- **Sandbox:** Docker, `--rm`, custom image, egress allowlist.
- **Verify:** headless browser (Playwright) for acceptance checks + screenshots.
- **Deploy v1:** `docker compose up` + a tunnel (e.g. cloudflared) → URL with no
  external account; Vercel adapter as the second deploy target.
- **State:** append-only JSON ledger on disk per run (`runs/<id>/ledger.jsonl`).

---

## 9. Risks & open questions

- **Codegen reliability at full autonomy.** Even Claude derails on novel multi-file
  builds. Mitigation: the gates + bounded repair loop catch most; honest-failure
  output prevents silent garbage. Open: what repair-attempt count balances cost vs
  success — tune against the fixture corpus.
- **Reference-website intake fidelity.** "Capture intent, not pixels" is fuzzy. Open:
  how structured the extracted `Brief` should be before it's good enough for `spec`.
- **Deploy adapter creep.** Each target is real integration work. v1 keeps it to one
  (tunnel) + maybe Vercel; resist more.
- **Egress allowlist maintenance.** Package installs pull from many hosts; the
  allowlist needs a sane default set or builds fail. Open: allowlist vs
  monitored-deny-by-default for v1.

---

## 10. Build order (milestones)

1. **Skeleton + sandbox.** `cli.py` + `orchestrator.py` running a no-op phase inside a
   disposable container, with the ledger and budget plumbing. Proves containment.
2. **Pipeline on a fixture, no deploy.** intake→spec→plan→build→verify against a hand-
   written PRD fixture; stop at "green build + acceptance checks pass" locally.
3. **Deploy + report.** Add the tunnel deploy adapter, screenshot, run-report; full
   `PRD → live URL` on the fixture.
4. **Reference-URL intake + repair loop hardening.** Add URL intake; tune the repair
   loop against a small fixture corpus.
5. **Second deploy adapter (Vercel) + cost dashboard.** Then revisit v2 (hybrid brain,
   more verticals).

Each milestone is its own plan → implement → verify cycle.
