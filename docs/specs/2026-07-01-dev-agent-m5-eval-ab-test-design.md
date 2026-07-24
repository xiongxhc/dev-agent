# M5 — Eval corpus + the A/B test

**Status:** harness built + unit-verified (2026-07-01). `devagent/eval/` (corpus, judge, scoring,
runner, report) + a `devagent eval <corpus>` subcommand ship with 23 unit tests (fakes — no Docker,
no tokens): freeze-once + arm fairness, N runs, resumability, graceful managed-arm skip, per-run
failure isolation, blinded judge, dual-cost scoring, report render. The live corpus run (real
builds/tokens — the actual A/B numbers) is a manual kickoff.
**Goal:** answer the two empirical questions the whole Executor seam exists to settle — **which
build arm produces better apps, and what does each cost** — with a repeatable, budget-bounded
harness over a small fixture corpus.

---

## The seam is already there

`executor.py` states the fairness rule outright: *"Everything else in the pipeline (intake, spec,
plan, verify, deploy, gates, eval) is SHARED; only the build EXECUTION ENGINE differs."* Both arms
already implement `Executor.build(BuildRequest) -> BuildResult` (`SdkExecutor`, `ManagedExecutor`),
selected by `Config.executor`. `BuildRequest.scope`/`.plan` are **frozen** ("identical bytes go to
both A/B arms"). `BuildVerifier` already produces a deterministic `VerifyReport` (rebuild-from-source
→ boot → acceptance), and both arms already report `cost_usd` (SDK: model-token cost; Managed:
model-token **+ wall-hours × SESSION_HR_USD**). M5 is the harness that drives this seam across a
corpus and tabulates the result — it adds an eval runner and a judge, not new pipeline surface.

## How it runs

For each fixture PRD:

1. **Freeze the brain once.** Run `ScopePhase → PlanPhase` a single time and snapshot the validated
   `scope.json` + `plan.json`. Both arms build from **these exact bytes** — so the comparison
   isolates the build engine, not scope/plan variance. (Scope/plan tokens are shared, excluded from
   the arm comparison.)
2. **Build each arm N times.** For arm ∈ {sdk, managed}, run `BuildPhase(executor=arm) → BuildVerifier`
   on the frozen `BuildRequest`, **N=2** times (the build is nondeterministic — N gives a variance
   signal). Each run is a real contained build; the existing per-run `Budget` bounds it.
3. **Score each build run** on three axes (below).
4. **Aggregate** into one eval report: per (fixture × arm) → acceptance pass-rate, judge score, cost
   (both normalizations), wall-clock; plus a corpus-level summary.

## What it measures

- **Deterministic acceptance (authoritative).** Straight from `VerifyReport`: `build_ok` (rebuilds
  from source with a frozen lockfile), `dist_present`, and the kind-dispatched acceptance checks
  (`checks_pass`). Binary per run; pass-rate over N. This is the objective floor — never a model
  judgment.
- **Blinded per-criterion LLM judge (qualitative).** A separate LLM scores the built repo against a
  fixed rubric (spec-completeness, code quality, UX/craft) 1–5 per criterion, **blinded to which arm
  produced it** (arm label stripped; runs shuffled). Adds a quality dimension binary acceptance
  can't see. Reported as indicative — acceptance stays authoritative.
- **Dual cost normalization.** The two arms have different cost structures, so report **both**:
  (a) **model-token cost** — `cost_usd` from token usage, apples-to-apples on the model spend;
  (b) **all-in cost** — token cost + session/compute time (Managed's session-hr is already folded
  into its `cost_usd`; the SDK arm's all-in adds its container wall-hours at the same rate). Plus
  raw tokens (in/out, cache-read) and wall-clock, all already on `BuildResult`.

## What's built

- **`devagent/eval/` + a `devagent eval` subcommand.** Reads a corpus manifest (list of fixture PRD
  paths + a config: arms, N), drives the freeze→build×N→score loop, writes `eval/<id>/` with
  per-run ledgers/reports (reusing the existing run machinery) and one `eval-report.html/.json`.
- **Fixture corpus** — ~5 PRDs, easy→hard, reusing/extending `examples/` (`hello`, `fullstack`,
  `fullstack-persistent`, `fullstack-persistent-shared`, + one auth/roles fixture now that M10
  exists). PRD-only.
- **The judge** — a `generate_structured` call with the blinded rubric → a validated score object.
- **Resumability** — cache each build run's result under `eval/<id>/`; a crash or a killed arm
  resumes without redoing completed runs (20 real builds is expensive).

## Risks / bounds

- **Cost & time.** 5 fixtures × 2 arms × N=2 = ~20 contained builds with real tokens. Opt-in, small
  corpus, resumable, each run under the existing `Budget`/`max_cost_usd`. The harness logs a
  cost/time estimate before starting.
- **Managed arm availability.** The beta sessions API may be gated/unavailable in a given env — the
  harness skips that arm gracefully and reports "arm unavailable" rather than failing the corpus.
- **Judge nondeterminism.** Blinded + per-criterion + low temperature; scores are indicative, the
  deterministic acceptance pass-rate is the headline quality number.
- **Fairness.** Freeze Scope+Plan per fixture; assert both arms receive byte-identical
  `BuildRequest.scope`/`.plan`.

## Deferred (no ceremony — just not in scope)

Reference-URL clones + SSIM visual scoring — needs URL intake, which doesn't exist yet. When URL
intake lands, the judge grows a visual-diff criterion; the harness shape is unchanged.

## Done when

- `devagent eval <corpus>` runs the full corpus, produces `eval-report.{html,json}` with per-fixture
  per-arm acceptance pass-rate, blinded judge scores, and dual-normalized cost + wall-clock.
- Both arms build from byte-identical frozen Scope+Plan per fixture (asserted).
- The run is resumable and bounded; the managed arm degrades gracefully when unavailable.
- The report answers, for this corpus: **which arm passes acceptance more often, scores higher
  blinded, and at what cost** — the M5 question.
