# dev-agent

A headless, autonomous **web-app builder**: drop a requirement — a PRD file on the CLI, or a
plain message in a Feishu chat — and it designs, builds, verifies, security-probes, repairs,
and deploys a working multi-service web app, unattended. A deterministic Python harness drives
bounded Claude calls (**LLM brain, deterministic hands**), with a deterministic gate between
every phase.

**Status:** chat-driven system builds are live end-to-end. One Feishu message runs
`architect → per-service builds → integration → security verify → repair → live preview`,
publishes the code to its own GitLab repo (M7), and follow-up messages in the same chat update
the running app in place (M25). Full history: [CHANGELOG.md](CHANGELOG.md).

---

## Quickstart

**Prerequisites**

- Python 3.11+ and Docker (the sandbox, verify, and preview containers)
- An Anthropic **API key** — billing is pay-per-token (~$0.15–0.50 per single build on Haiku,
  more for multi-service systems). Pro/Max subscription auth is **not permitted** for
  programmatic/headless use.

**Install**

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # add [feishu] for the chat bot
sandbox/build.sh                       # build the devagent-sandbox:m2 image (once; needs Docker)
mkdir -p ~/.config/local-agent-team
cp .env.example ~/.config/local-agent-team/dev-agent.env     # fill in; chmod 600
set -a; source ~/.config/local-agent-team/dev-agent.env; set +a
```

**First runs**

```bash
.venv/bin/python -m devagent.cli run examples/hello.md           # brain only: scope -> plan (cents)
.venv/bin/python -m devagent.cli run --build examples/hello.md   # + contained build -> verify -> preview URL
```

`--build` is opt-in because it requires Docker and spends build tokens; without it a run stops
after `plan`. Artifacts land in `runs/<id>/` — `scope.json`, `plan.json`, the append-only
`ledger.jsonl`, a self-contained `report.html`, and the built app under `out/`.

`examples/` are **BRD-level** requirement docs (see `examples/README.md`) — what the app must
do, never endpoints/stacks/env vars; designing those is the agent's job.

---

## Architecture

```
PRD/URL ─▶ scope ─▶ plan ─▶ [ Executor ] ─▶ verify ─▶ deploy ─▶ report
           └──────── shared, gated ────┘   │      └────── shared ──────┘
                                ┌──────────┴───────────┐
                          SdkExecutor              ManagedExecutor
                      (Agent SDK, your              (Managed Agents,
                       Docker sandbox)              hosted/self-hosted)
   a deterministic gate (schema-valid? build 0? routes 200?) sits after every phase
   scope → any recipe type (frontend, backend, fullstack, CLI, MCP, ...)
```

- **Deterministic harness** (`orchestrator.py`) owns sequencing, gates, budgets, and stop
  conditions — control flow is code, never the model.
- **Executor seam** (`executor.py`) is the one swappable component; everything else is shared.
  `BuildResult.success` is the executor's claim and is **not trusted** — gates re-check the
  produced repo on disk.
- **Contained builds** — disposable `docker run --rm` containers, egress restricted to
  api.anthropic.com + npm behind an allowlist proxy (`egress.py`), non-root, `out/` the only
  writable mount.

### Two layers — don't conflate them

1. **Runtime brain** — scope / plan / architect phases call the **Anthropic Messages API**
   (forced tool-use → validated pydantic). No code execution.
2. **Build executor** — the A/B seam where code actually gets written and compiled: arm A
   `SdkExecutor` (**Claude Agent SDK** in our Docker sandbox, the default) vs arm B
   `ManagedExecutor` (**Claude Managed Agents**, Anthropic-hosted sandbox). Pick with
   `DEVAGENT_EXECUTOR=sdk|managed`. The A/B comparison itself (M5) has a built harness
   (`devagent eval`) but the live baseline run is still pending.

(The repo is *developed with* Claude Code agent teams, but that's a dev-time tool — not part
of the product.)

### The single-run chain (the CLI primitive)

```
CLI  ──  devagent run --build <prd>  ──▶  orchestrator.py     (host; LLM-brain, deterministic hands)
        ┌─ scope ─ plan ─┬─────────── build ───────────┬─ deploy ─┐   gate after EVERY phase
        │  (Messages API)│        Executor seam         │          │
        │  on the host   │   SdkExecutor (default)      │          │
        │                │   → Docker sandbox + Agent   │          │
        │                │     SDK → Anthropic API      │          │
        │                │   (or ManagedExecutor, hosted)│         │
        │                ▼                               ▼          ▼
        │           rebuild-from-source verify      datastore?  preview container(s)
        │           + acceptance (boots the app,    (agent's    on 127.0.0.1:<port>
        │            persistence_survives_restart)   choice)         │
        └──────────────────────── ledger.jsonl (append-only event stream) ──────────────┘
```

Verify is the trusted re-check: it rebuilds the executor's output **from source** in a clean
container (`--frozen-lockfile`), boots it, and runs the scope's acceptance checks
(HTTP `route_status`, Playwright `selector_present`, auth-aware multi-actor flows, and
`persistence_survives_restart`). Failures feed a bounded repair loop (cap 2).

### The system lane (what Feishu uses)

`devagent build-system <prd.md>` wraps the single-run pipeline once per service. **The design
is decided ONCE**: the Architect's `SystemDesign` (service DAG + frozen contracts) is the only
scope authority — each sub-build gets a mechanically derived frozen scope, and both per-service
acceptance checks AND cross-service integration checks derive from the frozen contracts, so one
route can never be graded against two contradictory shapes.

```
PRD ─▶ architect ─▶ SystemDesign (service DAG + contracts) ─▶ [architect gate]
 ├─ per service, in dependency order (independent siblings concurrent · ONE shared budget):
 │      frozen scope ─▶ plan ─▶ build ─▶ verify      (the gated pipeline above, as a sub-run;
 │      datastore nodes skip the LLM build entirely — recipe image at bring-up)
 ├─ bring-up of the ACTUAL built targets on a per-run docker network
 │      · DATABASE_URL injected from design-level datastore deps · frontend config.json → live api
 ├─ cross-service E2E over real HTTP — the SAME derived checks ─▶ [integration gate]
 ├─ security verify (M24) — deterministic probes (mass-assignment, missing-authz, IDOR, ...)
 │      gate the run; LLM triage is advisory-only
 ├─ system repair loop (M23) — failing steps attributed to services, targeted re-invoke,
 │      fresh bring-up, full re-verify (bounded by DEVAGENT_MAX_SYSTEM_REPAIRS)
 └─ on SUCCESS the system STAYS UP as the preview · runs/<id>/system-report.json
    · git publishing (M7): each green service committed to the app's GitLab repo
```

**Persistence is the agent's decision** — the scope phase picks none / in-process SQLite / a
managed Postgres or Mongo datastore target per PRD. Verify holds every choice to the same bar:
`persistence_survives_restart` (write → restart the app while the datastore stays up → read
back). Deploy uses named volumes so preview data survives container restarts.

**Where it runs:** the agent, builds, verify, and previews are all local (your Docker); only
model inference goes to `api.anthropic.com`. The model weights never run locally.
`DEVAGENT_EXECUTOR=managed` flips the build itself to Anthropic's hosted sandbox — useful for
CI/shared runners with no docker-in-docker; pricier (session-hour charge on top of tokens).

**One-page visual overview:** [`docs/how-dev-agent-works.html`](docs/how-dev-agent-works.html).
Full design docs: [`docs/specs/`](docs/specs/).

### Modules

| Module | Job | Uses Claude? |
|---|---|---|
| `orchestrator.py` `tree.py` `budget.py` `ledger.py` `gates.py` `config.py` | harness: phase loop + recursive service tree, hard ceilings, audit ledger, deterministic gates, env-driven config | no |
| `schema.py` | pydantic `ProjectScope`/`Plan`/`SystemDesign` + contracts | no |
| `llm.py` | `generate_structured()` — Messages API forced tool-use → validated pydantic | **Messages API** |
| `phases/scope.py` `phases/plan.py` `phases/architect.py` | the brain: classify + clarify scope, task the plan, design the service DAG | **Messages API** |
| `executor.py` `phases/build.py` | the `Executor` Protocol seam; `BuildPhase` owns the build → verify → repair loop (cap 2) | via Executor |
| `executor_sdk.py` `sdk_runner.py` | arm A: contained Agent-SDK build; parallel per-target sessions (M12) | **Agent SDK** |
| `managed_executor.py` | arm B: builds on Claude Managed Agents, pulls the tarball back | **Managed Agents** |
| `verifier.py` `acceptance_runner.py` | rebuild-from-source + acceptance: HTTP/Playwright/auth-aware checks, restart-survival | no |
| `system_build.py` `contract_utils.py` `integration.py` | system lane: architect → sub-builds → bring-up → cross-service E2E; checks derived from frozen contracts; M23 repair loop | no |
| `security/` | M24 red-team: deterministic probes (gate) + LLM triage (advisory) | triage only |
| `design_diff.py` | M25 updates: mechanical prior-vs-new design diff → rebuild set + data fate | no |
| `gitops.py` | M7 publishing: lazy repo creation, per-green-service commits, finalize snapshot | no |
| `egress.py` `egress_proxy.py` | egress allowlist: internal network + CONNECT proxy (api.anthropic.com + npm only) | no |
| `deploy.py` `phases/deploy.py` `preview_server.py` | previews: datastore → backend → frontend bring-up, conn-URL injection, named volumes | no |
| `report.py` | self-contained `report.html` per run (phases, gates, cost, acceptance, preview URL) | no |
| `eval/` | M5 A/B harness: frozen fixtures, deterministic + blinded-judge + cost scoring | judge only |
| `recipes/` | open recipe registry; external JSON manifests + declarative toolchain images (M11) | no |
| `channels/` | Feishu app bot (inbound long-connection + outbound replies) | no |

---

## Usage

### CLI

```bash
python -m devagent.cli run <prd.md>                # brain only: scope -> plan
python -m devagent.cli run --build <prd.md>        # + build -> verify -> repair -> preview URL
python -m devagent.cli run --answers answers.md <prd.md>   # re-run with clarification answers

python -m devagent.cli build-system <prd.md>       # multi-service system from one PRD
python -m devagent.cli build-system --repo <url> <prd.md>  # publish into an existing repo
python -m devagent.cli update-system <run_dir> <change.md> # apply a change to a prior system, in place

python -m devagent.cli eval examples/corpus.json   # M5 A/B eval harness (real builds, real tokens)

DEVAGENT_EXECUTOR=managed  python -m devagent.cli run --build <prd.md>   # hosted build arm
DEVAGENT_BUILD_MODEL=claude-haiku-4-5-20251001 python -m devagent.cli run --build <prd.md>
#   cheap model for test builds; brain model is separate (DEVAGENT_LLM_MODEL)
```

### Feishu bot (the chat interface)

Drop a requirement into a Feishu DM (or @mention the bot in a group) → the bot spawns
`build-system` and streams live progress back into the chat (architect → per-service builds →
repair/security → preview URL + repo link). A follow-up message in the same chat **updates the
built app in place** (data preserved unless the change touches the data model); "new app" /
"start over" / "from scratch" (or their Chinese equivalents) force a fresh build. Help/greeting
messages get a usage card, not a build.

Setup: a Feishu **custom app** with Bot enabled, scopes `im:message` + `im:message:send_as_bot`,
event `im.message.receive_v1` in **long-connection mode** (no public URL needed), published and
added to a DM or group. Credentials go in `~/.config/local-agent-team/dev-agent.env`
(`FEISHU_APP_ID`/`FEISHU_APP_SECRET`; template in `.env.example`). Run it:

```bash
python -m devagent.channels.feishu_bot        # foreground, from the source checkout
bin/deploy-bot.sh                             # production: deploy-by-copy + restart
```

`bin/deploy-bot.sh` deploys the bot as a long-running service: it archives `master` into
`~/.local/share/local-agent-team/bot/src` (the bot never runs from the dev checkout), installs
into a persistent venv there, and restarts the process. `runs/` in the deploy dir persists
across deploys — **live previews bind-mount run dirs, so never delete it while apps are up**.
Note: the script currently archives the *monorepo* it lives in (`../..`) and installs
`src/dev-agent` — if you run from a standalone clone of this repo, adjust those two paths.

Design: [`docs/specs/2026-06-23-dev-agent-feishu-channel-design.md`](docs/specs/2026-06-23-dev-agent-feishu-channel-design.md).
(The legacy `channels/feishu.py` group-webhook is outbound-only and optional.)

### Git publishing (M7)

Set all three and every system build publishes to its own private GitLab project (created
lazily on the first green service; updates keep committing to the same repo):

```
DEVAGENT_GITLAB_URL=https://gitlab.example.com
DEVAGENT_GITLAB_TOKEN=<group access token, api scope, Maintainer>
DEVAGENT_GITLAB_GROUP=<group id or full path>
```

To publish into an **existing** repo instead, include its URL (same GitLab host) in the build
message — dev-agent branches off `develop` (or the default branch) as `devagent/<app>-<id>` and
never touches your branches. CLI: `build-system --repo <url>`.

Unset ⇒ publishing is off and the pipeline behaves exactly as before. Repo layout:
`services/<name>/` per service, `README.md`, `.devagent/` (prd, design + contracts, change
history). Publish failures never fail a build — the chat gets one ⚠️ line; the done message
carries `📦 Code: <repo url>`. The token is injected per push from the environment and never
written to disk. **The repo is a one-way projection**: dev-agent writes it, never reads it —
direct pushes to `services/` are replaced by the next chat-driven update (request changes via
chat instead).

---

## Configuration

All knobs are environment variables (see `config.py`; template in `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — (required) | pay-per-token key for all model calls |
| `DEVAGENT_EXECUTOR` | `sdk` | build arm: `sdk` (local Docker) or `managed` (hosted) |
| `DEVAGENT_LLM_MODEL` | `claude-sonnet-4-6` | brain model (scope / plan / architect) |
| `DEVAGENT_BUILD_MODEL` | Agent SDK default | SDK build executor model (e.g. Haiku for test builds) |
| `DEVAGENT_MANAGED_MODEL` | `claude-opus-4-8` | managed-arm model |
| `DEVAGENT_MAX_COST_USD` | `10` | hard $ ceiling per run; `0`/empty disables |
| `DEVAGENT_MAX_TOKENS` | `1000000` | runaway token ceiling per run |
| `DEVAGENT_MAX_SECONDS` | `1800` | wall-clock ceiling per run |
| `DEVAGENT_MAX_RETRIES` | `3` | shared retry budget across phases |
| `DEVAGENT_MAX_SYSTEM_REPAIRS` | `1` | system-lane repair-loop passes (own counter) |
| `DEVAGENT_BUILD_CONCURRENCY` | `3` | parallel per-target build cap: `min(targets, N)` |
| `DEVAGENT_EGRESS` | `1` | egress allowlist on build/verify containers (`0` disables) |
| `DEVAGENT_EGRESS_ALLOW` | api.anthropic.com + npm | comma-separated allowlist **override** for the proxy |
| `DEVAGENT_M2_IMAGE` | `devagent-sandbox:m2` | sandbox/runtime image name |
| `DEVAGENT_RECIPES_DIR` | unset | directory of external recipe manifests (M11) |
| `DEVAGENT_RUNS_DIR` | `runs` | where run artifacts land |
| `DEVAGENT_PREVIEW_PORT` | free port | fix the preview port instead of picking one |
| `DEVAGENT_MANAGED_SESSION_HR_USD` | `0.08` | managed-arm session-hour rate folded into cost |
| `DEVAGENT_SESSION_HR_USD` | `2.0` | eval only: machine-hour rate for all-in cost normalization |
| `DEVAGENT_GITLAB_URL` / `_TOKEN` / `_GROUP` | unset | M7 publishing (all three required; unset = off) |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | unset | Feishu app bot credentials |
| `FEISHU_WEBHOOK_URL` / `FEISHU_SECRET` | unset | legacy outbound group webhook (optional) |
| `DEVAGENT_FEISHU_RUNS_DIR` | `runs` | bot: base dir for per-chat runs |
| `SSL_CERT_FILE` | unset | combined CA bundle — a bare private CA breaks Feishu TLS (see Gotchas) |

## Testing

```bash
.venv/bin/python -m pytest -q                       # unit suite (no Docker, no tokens)
.venv/bin/python -m pytest -q -m docker             # containment + sandbox (needs Docker)
DEVAGENT_RUN_LIVE=1 .venv/bin/python -m pytest -q -m live   # live pipeline (spends tokens)
```

`DEVAGENT_REQUIRE_DOCKER=1` makes the containment suite **fail** rather than silently skip
when no Docker daemon is present (for CI).

## Sandbox image

The product runtime is the `devagent-sandbox:m2` image (node + Agent SDK + pnpm + Playwright);
the harness is just Python + a Docker daemon + an API key, so the loop runs anywhere Docker does.

```bash
sandbox/build.sh            # local: native-arch image into this machine's docker
REGISTRY=ghcr.io/<you>/devagent-sandbox sandbox/build.sh multiarch   # amd64+arm64, pushes
sandbox/build.sh recipes    # build external toolchain images from DEVAGENT_RECIPES_DIR manifests
```

---

## Limitations

- **Previews are local-only.** Deployed apps listen on `http://127.0.0.1:<port>` of the machine
  running the pipeline — the Feishu chat gets the URL, but it only opens on that host. Public
  deploys / CI-CD are the open M8 milestone.
- **API key required.** All model calls are pay-per-token; Pro/Max subscription auth is not
  permitted for headless use. Typical single builds run $0.15–0.50 (Haiku) — multi-service
  systems more; the `DEVAGENT_MAX_COST_USD` cap (default $10) is the backstop.
- **Greenfield only (for now).** Every build scaffolds a fresh app from a recipe; operating
  inside an existing repo / detecting its stack (Java/Maven etc.) is the open M9 milestone.
- **No URL intake.** "Clone this reference site" is designed-for but not built (reference-URL
  fixtures were deferred with M5's live run).
- **The A/B baseline hasn't been run.** Both executor arms are live-verified individually, but
  the M5 corpus eval that would pick a winner on data is a pending manual kickoff. The managed
  arm's Postgres/Mongo verify path is also not yet live-proven (SQLite is).
- **Schema-changing updates reset app data.** M25 updates preserve the datastore only when the
  `db_schema` contract is unchanged; migrations are a later milestone (the chat warns first).
- **Interactive OAuth/SAML consent can't be deterministically verified** — federated auth is
  verified against a mock IdP service; real-provider consent flows are flagged "not verified",
  never faked.

## Gotchas — read before operating

- **Never delete `runs/` (or the bot deploy's `runs/`) while previews are up** — live preview
  containers bind-mount `runs/<id>/out`; cleaning the directory breaks running apps.
- **On success, a system build STAYS UP as the preview** — containers, volumes, and a per-run
  docker network remain. That's the product (the chat's preview URL), but it accumulates:
  stopped/replaced runs are torn down, successful ones are not.
- **arm64 vs amd64:** a locally-built sandbox image on Apple Silicon is arm64-only and will
  silently fail to run on amd64 servers — use `sandbox/build.sh multiarch` (pushes to a
  registry; Docker limitation) when deploying elsewhere.
- **Private-CA forges and Feishu in one process:** setting `SSL_CERT_FILE` to just your forge's
  private CA breaks Feishu's TLS — use a **combined** bundle (public CAs + private CA), as noted
  in `.env.example`.
- **Managed-arm costs compound on repairs:** each repair pass re-runs a full hosted session
  (tokens + session-hours). The SDK arm is the sane default on a Docker-capable host.
- **`DEVAGENT_EGRESS_ALLOW` replaces the allowlist** (it doesn't extend it) — include
  api.anthropic.com and the npm registry if you override.
- **The token ceiling counts are calibrated** — cache-read tokens are cheap and excluded from
  budget counting (`BuildResult.budget_tokens`); if you lower `DEVAGENT_MAX_TOKENS` below ~1M,
  multi-target builds will falsely abort.
- **GitLab tokens:** the M7 publisher needs a group access token with `api` scope and Maintainer
  role; the token is env-injected per push and never written to disk.

## Roadmap

| Milestone | What | Status |
|---|---|---|
| M5 | live A/B corpus run (sdk vs managed baseline numbers) | harness ✅ · live run pending |
| M8 | ops: CI/CD + deploy per project (internal-ops-cli / generated `.gitlab-ci.yml` + k8s) | open |
| M9 | brownfield: operate inside existing repos, detect stack (Java/Maven), fit existing CI | open |
| M13 | team rollout (~10 users): concurrency, per-user isolation, shared service | open |
| M18/M19 | checkpoint/resume for days-long runs; mutable contracts / renegotiation | open |
| M22 | eval as change-gate: ledger telemetry + corpus replay before prompt/model changes | open |
| M26 | production-bug capture → GitLab issues → confirm-first fix | design ✅ ([spec](docs/specs/2026-07-16-dev-agent-m26-prod-bug-capture-design.md)) |

## Continuing development

- **Read the design docs first** — every milestone has a spec in [`docs/specs/`](docs/specs/)
  (authored in the monorepo's `docs/planning/dev-agent/specs/`; `bin/sync-docs.sh` refreshes
  the copies). The [CHANGELOG](CHANGELOG.md) records what each live run proved and which
  calibration fixes it forced — read it before re-tuning budgets, gates, or prompts.
- **The invariants to preserve:** control flow is code, never the model; every phase is followed
  by a deterministic gate; executor claims are re-verified from source; the system design is
  decided once and checks derive from its frozen contracts; only deterministic probe findings
  gate (LLM triage is advisory).
- **Extending:** new stacks/languages are recipe manifests (`DEVAGENT_RECIPES_DIR`, M11 — no
  code changes); new auth styles are dispatch-table entries; a new build backend is one
  `Executor` implementation behind the seam.
- **Tests mirror the cost model:** unit (free) → `-m docker` (local containers) → `-m live`
  (real tokens, opt-in via `DEVAGENT_RUN_LIVE=1`). CI should set `DEVAGENT_REQUIRE_DOCKER=1`.
