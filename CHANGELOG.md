# dev-agent — milestone log

The engineering journal: what shipped in each milestone, what the live runs proved, and the
calibration fixes they surfaced. Newest state is summarized in the [README](README.md); full
designs live in [`docs/specs/`](docs/specs/). ✅ shipped · ◐ partial · ⬜ open.

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
    Docker/tokens).
  - ✅ **deterministic build re-verification** + **repair loop** (`BuildVerifier` + `VerifyGate`,
    loop in `BuildPhase`): rebuilds the executor's output FROM SOURCE in a clean container
    (`rm -rf dist && pnpm install --frozen-lockfile && pnpm build`) — `--frozen-lockfile` is
    the pinned-deps check; `rm -rf dist` ensures a real build produced the bundle. On failure
    the executor is re-invoked with the verify diagnostics (`VerifyReport.log_tail` → the
    runner's prompt), cap 2, each spending a shared-Budget retry. Needs no API key (runs no
    model).
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
- **M4** ✅ — `ManagedExecutor` (Managed Agents, arm B) behind the same Executor seam.
  - ✅ **built + unit-verified** (`managed_executor.py`): builds the app on **Claude Managed
    Agents** (a hosted cloud sandbox via `beta.{agents,environments,sessions,files}`, beta
    `managed-agents-2026-04-01`, billed token rates + $0.08/session-hr) instead of our Docker.
    Flow: create agent (`agent_toolset_20260401`) → cloud env → session → stream the Spec+Plan
    to `session.status_idle` → the agent **tars the project to `/mnt/session/outputs/app.tar.gz`**
    → pull it via `files.list(scope_id=session)` + `files.download` → extract to `workdir/out`
    → delete session. The **shared** verify/acceptance/repair then re-checks the pulled output in
    our egress-contained Docker — so the A/B stays fair. Select the arm with `DEVAGENT_EXECUTOR=
    managed` (default `sdk`).
  - **De-risk resolved (2026-06-24, 6 live probes):** outputs MUST go under `/mnt/session/outputs/`
    (elsewhere is ephemeral) AND via **bash** (the model's `write` tool didn't reliably target it).
    A diagnostic where the agent `ls`-verified proved the round-trip: file landed, `files.list`
    surfaced it immediately, download matched. Hence the tarball-via-bash design.
  - ✅ **live-verified (2026-06-24):** a real `DEVAGENT_EXECUTOR=managed --build` built a
    Vite+React+Tailwind app on Managed Agents, tarred it, we pulled + extracted it, the shared
    rebuild-from-source passed, and acceptance passed (`route_status /` 200 + `selector_present h1`).
    First run surfaced one bug — the arm must write `out/.devagent/spec.json` for the shared
    acceptance runner (SdkExecutor did; this didn't) — fixed + regression-tested. Note: the repair
    loop re-ran the managed build twice on that (spurious) failure, so a managed run is pricier
    than the sdk arm — capping/skipping repairs for the managed arm is worth considering.
  - ✅ **managed token/cost captured** (2026-06-30): `_drain` accumulates the events' `usage` +
    any reported `cost_usd` (defensive on the not-yet-frozen event schema), and `_cost_fields` adds
    the wall-clock **session-hour charge** (`wall/3600 * SESSION_HR_USD`, default $0.08,
    `DEVAGENT_MANAGED_SESSION_HR_USD`) — the cost component the SDK arm lacks. Both success and
    no-tarball `BuildResult`s now carry `tokens_in/out` + `cost_usd`, so the A/B cost comparison
    (M5) has honest managed numbers. **M4 done** (still bears the earlier-noted managed-repair cost
    caveat; capping managed repairs is an M5-time tuning call).
  - **When the managed arm actually matters (the case for keeping it).** On the current setup — a
    local Docker-capable host, one operator, cost-sensitive — the **sdk arm is the right default**
    and managed's session-hr premium isn't worth it. Managed earns its place precisely when the
    pipeline stops running on a machine you control with Docker:
    - **CI / the ops platform (M8).** Builds triggered inside GitLab CI or on the Rancher/k8s
      ops platform need **docker-in-docker / privileged containers** for the sdk arm — often
      disallowed or a security no-go on shared runners. The managed arm builds in Anthropic's cloud;
      the CI job just orchestrates and pulls the tarball. This is where M8 (CI/CD + deploy) is headed.
    - **Concurrency beyond the host.** The sdk parallel build (M12) is capped at
      `min(targets, 3)` because each target is a container + chromium on *our* box; a busy Feishu bot
      (many PRDs / many targets) makes the host the bottleneck. Managed offloads concurrency to the
      cloud.
    - **A shared service, not a laptop.** As a team service, managed keeps it near-stateless — no
      per-host Docker/capacity provisioning, no sandbox image shipped to every host.
    - **Toolchains not baked into our image.** An exotic runtime means building a new
      `devagent-sandbox` image (M11 toolchain images); managed's `agent_toolset` + unrestricted-net
      cloud env may already cover it — less image maintenance.
    - **Untrusted build steps off our infra.** The build runs generated code + `pnpm install` from
      the net; on a production host, running that in Anthropic's sandbox instead of ours can be
      preferable.

    **Plan:** keep the managed arm in the seam (already built, zero ongoing cost), run the M5 A/B
    **once** for a baseline quality/cost data point, then default routine runs to **sdk-only**
    (`"arms": ["sdk"]` in the corpus). Revisit when M8 puts builds in CI / the ops platform.
- **Execution order: M6 → M5 → M7 → M8 → M9.** (M5 reorder decided 2026-06-24; the old monolithic
  M7 was split into M7/M8/M9 along the bind → CI → deploy seam on 2026-06-25.) M5's A/B was
  reordered to run *after* M6 so the eval measures the **real full-stack workload** (frontend +
  backend + CLI), not a toy frontend-only corpus — its whole job is to pick the executor arm, and
  that choice is only as good as the workload it's measured on. The executor seam makes M6 mostly
  shared across both arms, so "build on both then decide" costs little, and it avoids paying twice
  for the test.
- **M6** ✅ *(complete — live-verified 2026-06-25)* —
  **Flexible scope-first builder.** Inverted the pipeline: a new **Scope phase** turns *any*
  request into a confirmed, flexible `ProjectScope` — deliverable type(s), stack, and repo-or-not
  are **request-driven** (backend-only, MCP, frontend-only, fullstack, any language). Ambiguity
  triggers a clarification loop (Feishu out / `--answers` in). Everything downstream dispatches
  on the scope via an open **recipe registry**; toolchain is provisioned per project. Ships two
  recipes: `node-vite-react` (frontend) + `node-express` (backend). CLI rewired:
  `scope → plan [→ build → verify → deploy]`; `intake`/`spec` phases and `Brief`/`Spec`/`BriefGate`/
  `SpecGate` retired. Both A/B arms inherit the new pipeline. **Live-verified:** a real Express
  API + Vite/React frontend monorepo built, both targets rebuilt-from-source, backend booted +
  all 6 per-target acceptance checks green, deployed (`DEVAGENT_RUN_LIVE=1`, ~$0.13/~4 min). The
  first run surfaced a real calibration fix — the default `max_tokens` ceiling was raised
  200k→1M (it counts cache-read tokens, and a 2-target build is ~320k+). Design:
  [`docs/specs/2026-06-24-dev-agent-m6-flexible-scope-builder-design.md`](docs/specs/2026-06-24-dev-agent-m6-flexible-scope-builder-design.md).
  - **Persistence seam (agent-decided storage)** ✅ *(live-verified 2026-06-25)* — storage is
    the agent's per-PRD decision (none / SQLite / managed Postgres+Mongo datastore targets as
    service recipes); verify brings up a real sibling datastore container, injects the conn URL,
    and holds every choice to `persistence_survives_restart` (write → restart the app while the
    datastore stays up → read back). **Live-proven** on the durability tasks PRD: the agent chose
    SQLite, all 8 checks green incl. restart-survival, deployed — ~$0.53, ~10 min. Surfaced a
    real calibration fix: the runaway token ceiling was double-counting cache-read tokens, so a
    legitimate cache-heavy build (1.42M token_in, 1.33M cache-read) falsely aborted at 1M; the
    budget now counts expensive tokens only (`BuildResult.budget_tokens`). Design:
    [`docs/specs/2026-06-25-dev-agent-persistence-datastore-seam-design.md`](docs/specs/2026-06-25-dev-agent-persistence-datastore-seam-design.md).
    Follow-ups closed along the way:
    - ⬜ **live-validate the managed-datastore path** — only SQLite is live-proven; the
      sibling-container Postgres/Mongo verify+deploy path still needs a live run.
    - ✅ **`max_cost_usd` ceiling** — a per-run dollar guard (uses the SDK's exact `cost_usd`)
      as the project-agnostic runaway bound. Default **$10** (`DEVAGENT_MAX_COST_USD`; raise for
      prod, `0` disables).
    - ✅ **auth-aware acceptance** *(live-verified 2026-06-30)* — `AcceptanceCheck.auth` + an
      optional `ArtifactSpec.auth` `AuthFlow`; the runner logs in once and sends
      `Authorization: Bearer <token>` on `auth: true` checks. Three seams kept consistent
      (scope prompt declares the flow; `enrich_scope` carries it in-container; the build prompt
      surfaces the exact register/login bodies as a hard contract). **Live-proven:** the full
      todo+login+logout PRD built, logged in, passed `/auth/me` + auth-gated
      `persistence_survives_restart`, deployed — **$0.15 on Haiku**.
    - ✅ **configurable build model** (`DEVAGENT_BUILD_MODEL`) — the SDK build executor's model
      as a per-run knob, the second A/B axis (model quality/cost) orthogonal to the executor arm.
    - ✅ **hardened `preview_server.py`** — was a single-threaded `TCPServer` that died on a
      hung/dropped client; now `ThreadingHTTPServer` + per-request error swallow + a supervisor
      loop that rebinds on any crash, and deploy runs preview/datastore containers with
      `--restart unless-stopped`.
- **Feishu channel** ✅ *(shipped + live-verified 2026-06-30; design
  [`docs/specs/2026-06-23-dev-agent-feishu-channel-design.md`](docs/specs/2026-06-23-dev-agent-feishu-channel-design.md))* —
  a **single Feishu app bot** (`channels/feishu_bot.py` + `feishu_app.py`) where a member drops a PRD
  into a DM (or @mentions the bot in a group) → autonomous build → **preview URL + report posted
  back in-thread**, with **live phase-by-phase progress** streamed from the ledger as the run executes.
  Inbound is an `im.message.receive_v1` event subscription over a WebSocket long-connection (no public
  URL); outbound replies via the app message API. **Live-proven:** a chat message drove a full
  scope→plan→build→deploy run with progress streamed back. (The older `channels/feishu.py` group-webhook
  is outbound-only and now superseded.)
- **M5** ◐ *(harness built + unit-verified 2026-07-01 — design
  [`docs/specs/2026-07-01-dev-agent-m5-eval-ab-test-design.md`](docs/specs/2026-07-01-dev-agent-m5-eval-ab-test-design.md))* —
  **eval corpus + the A/B test** (the two empirical unknowns: quality, cost). `devagent eval
  <corpus>` freezes scope+plan ONCE per fixture (both arms build byte-identical bytes — the
  fairness rule), builds each arm **N=2×**, and scores each run on three axes: **deterministic
  acceptance** (from `VerifyReport` — authoritative), a **blinded per-criterion LLM judge**
  (spec-completeness / code-quality / craft, arm label stripped), and **dual-normalized cost**
  (model-token vs all-in incl. session-hr). Resumable under `runs/eval/<id>/`; the managed arm
  degrades gracefully when its API is unreachable. Corpus is a JSON manifest (`examples/corpus.json`:
  3 fixtures easy→hard incl. an auth/roles one). ✅ **harness + judge + report unit-verified**
  (fakes, no Docker/tokens); ⬜ **the live corpus run** (real builds, real tokens — the actual A/B
  numbers) is a manual kickoff. Reference-URL clones + SSIM deferred (no URL intake yet).
- **M10** ✅ *(built + unit-verified 2026-06-30)* — **Auth & access depth (model-declared, not
  hardcoded).** Widened the auth *vocabulary* (never per-app code) so the scope model declares
  richer auth and the runner executes whatever it declared:
  - ✅ **Sessions / cookie auth** — `AuthFlow.mode = bearer|cookie`; the runner captures `Set-Cookie`
    at login and replays `Cookie` on later checks (`acceptance_runner._cred_cookie`). Cookie mode
    needs no `token_json_path`.
  - ✅ **Roles / permissions (authz)** — the single flow generalizes to named **actors**
    (`ArtifactSpec.actors`, each an `AuthFlow` with a unique `name` + `role`); checks carry
    `as: <actor>` + `expected_status`, so the model declares a real permission matrix
    (`as: admin → /admin → 200`; `as: member → /admin → 403`). The runner logs in as each actor
    once and asserts the status per actor; the build prompt gets the actor contracts as the matrix.
  - ✅ **Federated / third-party (LDAP, OIDC, SAML)** — verify runs sealed, so a real provider is
    unreachable **by design**. Handled via the **service-recipe seam**: the backend declares
    `detail.idp` (a seeded mock-IdP **service** target) + `detail.idp_env`; verify stands it up on
    the per-run network and injects `<idp_env>=<issuer-url>` so the app authenticates against *that*
    (mirrors the datastore injection). Real interactive OAuth/SAML consent is **not** deterministically
    verifiable — the scope prompt flags "not verified", never fakes a pass. *(Live bring-up of a
    specific mock-IdP image is the natural next step; the seam + injection are unit-verified.)*
- **M11** ✅ *(complete 2026-06-30)* — **Declarative extensibility — add languages/recipes without
  editing dev-agent.**
  - ✅ **declarative recipe loading**: `recipe_from_dict` + `load_external_recipes` parse `*.json`
    recipe manifests from `DEVAGENT_RECIPES_DIR` (each a recipe dict or a list: `toolchain.image`,
    `build_cmd`, `artifact_glob`, `boot`, `supported_checks`, optional `service` spec) and merge them
    into `REGISTRY` at import — a manifest with a built-in's name overrides it; a malformed manifest
    fails loudly (naming the file). Because the scope catalog iterates the registry, a dropped manifest
    is **immediately offered to the model** and the whole pipeline dispatches on it with **no code
    change**. Proven: a `python-flask.json` registered and appeared in the scope catalog.
  - ✅ **toolchain images**: a manifest can now ship a NEW toolchain as data — `toolchain.dockerfile`
    names a Dockerfile (relative to the recipes dir) that builds `toolchain.image`.
    `devagent.recipes.toolchains.toolchain_build_specs` maps manifests → docker-build specs (deduped
    by image) and `build_all` runs them; `sandbox/build.sh recipes` is the operator entrypoint.
    Prebuilt toolchains (the bundled `devagent-sandbox:m2`, node + python3) declare no `dockerfile`
    and need no build.
  - ✅ **declarative auth styles**: closed by M10 — the runner dispatches on `AuthFlow.mode` via the
    `_CRED_BUILDERS` table against the open `KNOWN_AUTH_MODES` vocabulary, so an auth style is a
    dispatch-table entry (data-not-code), exactly like a stack is a recipe.
- **M12** ✅ *(built + unit-verified 2026-06-30 — design
  [`docs/specs/2026-06-30-dev-agent-m12-parallel-team-build-design.md`](docs/specs/2026-06-30-dev-agent-m12-parallel-team-build-design.md))* —
  **Parallel / team build.** `PlanGate` already enforces **pairwise-disjoint file ownership**
  *precisely so parallel build agents never collide*. M12 uses the seam: a **fresh** build whose
  plan **cleanly partitions** across its `kind=="build"` targets (every target owns ≥1 task, every
  task in exactly one target) runs **one contained SDK session per target concurrently** (web ∥ api,
  each its own container/`node_modules`/build, all mounting the shared `/out` — writes disjoint by
  target dir). `SdkExecutor.build()` writes a per-target scope + plan slice, runs them on a worker
  pool capped at `min(targets, DEVAGENT_BUILD_CONCURRENCY|3)`, and aggregates into one `BuildResult`
  (tokens/cost **summed**, `wall_clock_s` = **max**, failures surfaced in target order). Any plan
  that doesn't cleanly partition (a shared/root task, a cross-target task, a target with no tasks),
  a **repair pass**, and single-target scopes all take the original **sequential** single session —
  so the disjoint-`/out` invariant is *guaranteed*, never assumed. The full enriched scope is still
  written to `.devagent/scope.json`, so **verify/acceptance/deploy and the repair loop are untouched**
  — parallelism is internal to the Executor seam. *(Follow-up: a large, coupled single target fanning
  out to a lead agent + subagents on its disjoint files — the intra-target agent-team pattern — needs
  the in-container SDK's Task tool and a live run to verify; the concurrent-target capability is the
  headline deliverable and is shipped.)*
- **M14–M19** — **Long-horizon multi-service builder** (design
  [`docs/specs/2026-07-02-dev-agent-long-horizon-builder-design.md`](docs/specs/2026-07-02-dev-agent-long-horizon-builder-design.md)).
  One PRD → dev-agent **designs the services itself** and builds a whole system over a
  long-horizon autonomous run — **N bounded sub-builds under a durable architecture**, not one
  flat mega-build. The spine (M14 Architect → M15 recursive orchestrator → M16 contract
  injection/conformance → M17 integration verification) shipped via M20/M21 below; M18
  (checkpoint/resume + cost governance) and M19 (mutable contracts / renegotiation) remain open.
- **M20** ✅ — **system-build wiring.** `devagent build-system <prd.md>` runs end-to-end:
  Architect → per-service sub-builds (M15 tree, one shared Budget) → docker bring-up on a
  per-run `devagent-sys-<run_id>` network → cross-service E2E (M17) →
  `runs/<id>/system-report.json`, with teardown on every path (including exceptions). Design:
  [`docs/specs/2026-07-02-dev-agent-system-build-wiring-design.md`](docs/specs/2026-07-02-dev-agent-system-build-wiring-design.md).
- **M21** ✅ — system-build live path: datastore nodes from recipe images (no LLM build),
  deploy-less sub-runs (no preview collisions), consumed-contract injection into sub-builds,
  bring-up wired from each sub-run's scope.json via the extracted `deploy.wire_targets`
  (conn env + frontend apiBase + per-node namespacing). Design:
  [`docs/specs/2026-07-02-dev-agent-m21-system-live-path-design.md`](docs/specs/2026-07-02-dev-agent-m21-system-live-path-design.md).
- **One-flow** ✅ (2026-07-03) — **the design is decided once; checks derive from contracts.**
  Live Team Polls runs failed 4/4 because THREE independent LLM calls specced the same route
  (architect integration checks, each sub-run's re-scope, the contract) and contradicted each
  other — no build could satisfy all. Fixes: sub-builds get a FROZEN mechanical scope
  (`system_build.scope_for_node` + `FrozenScopePhase`, per-service ScopePhase deleted from the
  lane); acceptance AND integration checks are derived from the frozen contracts
  (`contract_utils.derive_checks` / `derive_integration_checks`, architect prose checks used
  only as fallback); success keeps the brought-up system as the preview (urls in report +
  ledger `system_deploy`); design datastore nodes get per-run container names; the ledger's
  final `system_build_end` is post-integration truth (tree verdict renamed `tree_build_end`).
  Review: [`docs/specs/2026-07-03-dev-agent-single-flow-review.md`](docs/specs/2026-07-03-dev-agent-single-flow-review.md).
- **M23** ✅ — **system-level repair loop.** On cross-service verification failure, `build_system`
  deterministically attributes failing steps to their build-kind nodes (`implicated_nodes`) and
  re-invokes the executor on each node's existing `out/` with the full report as `repair_context`
  (via `run_node`'s seam; frozen scope + persisted plan reloaded through `FrozenPlanPhase`, no
  re-plan), then tears down, brings the system up fresh, and re-verifies the full suite
  (integration + security once M24 lands). Bounded by its own counter, `max_system_repairs`
  (default 1) — not the shared retry budget. `SystemReport.repairs` records every pass; ledger
  events `system_repair_start`/`system_repair_end` trace it.
  Design: [`docs/specs/2026-07-06-dev-agent-system-repair-loop-design.md`](docs/specs/2026-07-06-dev-agent-system-repair-loop-design.md).
- **M24** ✅ — **security verify phase (red-team).** Functional checks verify the contract was
  *met*, never that it's *safe* — a live run shipped `POST /auth/register` accepting `role=admin`
  (privilege escalation) with every check green. `SecurityVerifyPhase` probes the brought-up
  preview (`base_urls` + the synthesized `AuthFlow`, plus a second registered principal for
  IDOR/authz) with a deterministic probe library keyed off the frozen contract — mass-assignment,
  missing-authz, cross-user IDOR, weak-registration, verb-tampering — then a fail-safe LLM triage
  pass expands/classifies (it never gates on its own; the deterministic findings still gate when
  the triage API is down). Provenance is enforced, not assumed: every `Finding` carries a
  `source` (`probe` | `triage`), `triage()` stamps its own emissions `triage`, and only `probe`
  findings can gate — so triage's spec-reading speculation surfaces as advisory but never fails a
  build (a live run failed a *flawless* app to `integration_failed` when triage findings gated;
  because that speculation is a function of the frozen contract, it re-fired every re-verify and
  made the M23 repair loop unwinnable). A gating ruleset partitions the findings: `GATING_KINDS`
  (mass_assignment, missing_authz, idor) fail the run, with a `(route, probe-class)` escape hatch
  for a design-declared intentionally-open pair — `mass_assignment` is never escape-hatchable, so
  the July-3 bug can't be waived away. Gating findings render as M23 failing steps and feed its
  repair loop through `build_system`'s `security_verify` seam (`reverify` runs the combined
  integration+security suite on the initial pass and every post-repair pass); the single-run path
  (`devagent run --build`) has no repair loop, so it reports the gate and fails rather than
  auto-repairing. `SystemReport.findings` carries the run's full findings list (gating and
  advisory) regardless of outcome. Regression-guarded: `test_security_probes.py` fixtures pin the
  July-3 vulnerable app (reflected `role=admin` → exactly one `mass_assignment` gating finding)
  and a safe app (no reflection, protected routes 401 → zero gating findings). Depends on M23.
  Design: [`docs/specs/2026-07-06-dev-agent-m24-security-verify-phase-design.md`](docs/specs/2026-07-06-dev-agent-m24-security-verify-phase-design.md).
- **M25** ✅ *(built + unit-verified 2026-07-14)* — **iterative updates (chat-stateful
  refinement).** A follow-up chat message modifies the app already built in that chat instead of
  building a fresh one. The bot maps `chat_id → prior run dir`; the architect gets an update mode
  (prior `SystemDesign` + change → new design); a mechanical design diff selects only the changed
  services, which rebuild in place (re-plan + build in the existing `out/`, the M23 repair engine
  driven by a change request instead of a test failure); then the existing bring-up + reverify
  loop redeploys. Data continuity is gated on a `db_schema` contract diff: **code-only updates
  preserve the datastore; schema-changing updates reset it with an explicit chat warning** —
  data-preserving schema migration (a generated-app migration runner + gate) is deliberately a
  later milestone. Depends on M23.
  Design: [`docs/specs/2026-07-13-dev-agent-m25-iterative-updates-design.md`](docs/specs/2026-07-13-dev-agent-m25-iterative-updates-design.md).
  Shipped: `runs/chat-apps.json` chat→run binding with escape-word detection (English + Chinese)
  and per-chat serialization; `design_diff.py`'s mechanical prior-vs-new diff picking the rebuild
  set (changed services + their consumers, topo-ordered) and data fate; in-place selective rebuild
  via the `UPDATE_PREFIX` build context, sharing the extracted `_verify_repair_deploy` loop with
  `build_system`; volume preservation keyed off the `db_schema` contract; and the standalone
  `devagent.cli update-system` entrypoint.
- **M7** ✅ *(shipped 2026-07-16 — design
  [`docs/specs/2026-07-16-dev-agent-m7-git-publish-design.md`](docs/specs/2026-07-16-dev-agent-m7-git-publish-design.md))* —
  **git publishing (durable repo accretion).** Built systems stop being throwaway `runs/` output:
  with `DEVAGENT_GITLAB_URL`/`TOKEN`/`GROUP` set, every system build publishes to its own private
  GitLab project, created lazily on the first green service; updates keep committing to the same
  repo. Shipped as `gitops.py` (`ForgeClient` + `GitPublisher`): per-green-service commits during
  builds/repairs (publish path serialized — `TreeOrchestrator` runs sibling `run_node`s
  concurrently), a `finalize()` deliverable snapshot (sync, README, `.devagent/` metadata: prd,
  design + contracts, change history), builds bindable to an **existing** repo (URL in the build
  message or `build-system --repo <url>`; branches off `develop`/default as `devagent/<app>-<id>`,
  never touching your branches), and the Feishu done-message carrying `📦 Code: <repo url>`.
  Publish failures never fail a build (one ⚠️ chat line). Dormancy + token hygiene pinned by
  tests: unset ⇒ the pipeline behaves exactly as before; the token is injected per push from the
  environment and never written to disk. Live-litmus fix: forge-reported URLs are rebased onto the
  `DEVAGENT_GITLAB_URL` host (the forge self-reports its internal hostname).
- **M26** ⬜ *(design approved 2026-07-16 — design
  [`docs/specs/2026-07-16-dev-agent-m26-prod-bug-capture-design.md`](docs/specs/2026-07-16-dev-agent-m26-prod-bug-capture-design.md))* —
  **production-bug capture → GitLab issues → confirm-first fix.** A preview monitor in the bot
  process watches every chat-bound app (container state, error logs, health probes, plus `bug: ...`
  chat reports), fingerprints findings, and files deduplicated issues in the app's M7 repo with
  evidence at detection time; the M25 update loop is the one-command fix path. Confirm-first:
  the monitor never spends tokens or redeploys unprompted. Depends on M7.
