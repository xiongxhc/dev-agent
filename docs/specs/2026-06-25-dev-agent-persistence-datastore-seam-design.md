# DevAgent — Agent-Decided Persistence (datastore seam) (design)

**Date:** 2026-06-25
**Status:** implemented (see docs/planning/dev-agent/plans/2026-06-25-dev-agent-persistence-datastore-seam.md)
**Builds on:** [`2026-06-24-dev-agent-m6-flexible-scope-builder-design.md`](./2026-06-24-dev-agent-m6-flexible-scope-builder-design.md)
**Depends on:** M6 (done). Independent of M5/M7/M8/M9 — sequence later.

---

## 0. One-line conclusion

A built app's storage is **the agent's decision, made per-PRD**: none, an in-process file
(SQLite), or a managed service (Postgres / a NoSQL store) — same build or a separate target.
The PRD demands an **outcome** ("data survives a restart"); the Scope-phase LLM picks the
engine. DevAgent's job is to make *every* choice **buildable, source-verifiable, and
deployable** — which means extending the recipe seam to **service recipes** and making
**verify run a real sibling datastore container**, so the managed-service path is held to the
same rebuild-from-source → boot → acceptance bar as everything else. (This is the **5b**
scope — the full seam — chosen over the SQLite-only **5a** cut because 5a would leave exactly
the engine the operator named first, Postgres/NoSQL, verify-blind.)

---

## 1. Why (the corrections this design encodes)

- **The operator does not choose the storage engine; the agent does, case by case.** Literal
  framing (2026-06-25): *"it can be a postgres, it can also be NoSQL, it can also decide to be
  in the same or separate build — a case-by-case decision by the agent SDK linking with the
  API. So don't ask me."* The PRD states a durability requirement; the Scope phase classifies
  the implementation.
- **Source-verification is non-negotiable, including for the managed-service path.** DevAgent's
  reason to exist is the verify gate (rebuild from source → boot → machine-checkable
  acceptance). A design where "agent picks Postgres" deploys but is never source-verified
  betrays that invariant. Therefore verify must stand up a real datastore — the **5b** scope.
- **SQLite is not a recipe.** It is in-process code the backend writes to a file; it needs no
  service container. Only *managed* datastores (Postgres, Mongo) are service recipes.
- **The restart that proves persistence restarts the APP, not the datastore.** State living in
  the datastore is the whole point; killing and relaunching the *app* while the datastore stays
  up is what distinguishes real persistence from an in-memory array.

---

## 2. Scope

**In:** recipe-registry "service" flavor; two managed datastore recipes (`postgres`, `mongo`);
Scope-phase awareness that persistence is the agent's call; a backend→datastore dependency
declaration; deploy wiring (datastore → backend → frontend ordering, connection-env injection,
named volumes); a new `persistence_survives_restart` acceptance check; verify reworked to bring
up sibling datastore containers networked to the in-container boot; one fixture
(`examples/fullstack-persistent.md`) + a `DEVAGENT_RUN_LIVE` test.

**Out:** managed cloud databases (RDS etc.) — that's an M8 ops concern; schema-migration
*tooling* opinions (the agent writes whatever DDL/migration its stack uses); multi-database /
sharding; NoSQL beyond a single registered Mongo recipe (the seam generalizes, but we register
two engines, not a zoo).

---

## 3. Architecture — by phase

### 3.1 Recipe registry: two flavors

Add a discriminator to `Recipe`:

```python
kind: str = "build"          # "build" | "service"
service: ServiceSpec | None = None
```

`ServiceSpec` (new, frozen) carries everything to run a stock datastore image as a sibling
container — no source, no build:

```python
@dataclass(frozen=True)
class ServiceSpec:
    image: str                       # e.g. "postgres:16-alpine"
    port: int                        # in-container port, e.g. 5432
    env: tuple[tuple[str, str], ...] # image config, e.g. (("POSTGRES_USER","devagent"),...)
    volume_path: str                 # container dir to persist, e.g. "/var/lib/postgresql/data"
    ready_cmd: tuple[str, ...]       # exec'd in the container for readiness, e.g. ("pg_isready",...)
    conn_url_template: str           # "postgresql://devagent:devagent@{host}:{port}/app"
    ready_timeout_s: float = 60.0
```

For service recipes `scaffold_hint` / `build_cmd` / `artifact_glob` / `boot` are `None`/unused.
`supported_checks` is empty (a datastore carries no acceptance checks of its own).

Register two:

- `postgres` — `postgres:16-alpine`, `pg_isready`, `postgresql://...@{host}:{port}/app`.
- `mongo` — `mongo:7`, a TCP/`mongosh --eval 'db.runCommand({ping:1})'` readiness, `mongodb://{host}:{port}/app`.

The backend `node-express` recipe gains `persistence_survives_restart` in `supported_checks`.

### 3.2 Schema additions

- `AcceptanceKind` gains `"persistence_survives_restart"`.
- `AcceptanceCheck` gains the fields that check needs (reuses `route`/`method`/`body` for the
  write, plus `json_path` to locate the created id, and a `verify_route` to read it back).
- `ArtifactSpec.detail` convention for a backend that depends on a datastore:
  `{"datastore": "<datastore-target-name>", "conn_env": "DATABASE_URL"}`. SQLite path: the
  backend declares `{"persist_path": "data/app.db"}` and no datastore target.

### 3.3 Scope phase

Service recipes appear in the catalog. Prompt addition (the agent's decision):

> Persistence is your call. If the spec needs durable state, either (a) persist to a file
> inside the target (e.g. SQLite at `data/<x>.db`) — no extra target — or (b) add a datastore
> target (`postgres`/`mongo`) and set the dependent backend's `detail.datastore` to its name
> and `detail.conn_env` to the env var your code reads the connection URL from. Add a
> `persistence_survives_restart` check on the backend either way.

`ScopeGate`: allow a `kind == "service"` target to have empty `acceptance_checks`; validate
that any `detail.datastore` names a real service target in the same scope.

### 3.4 Build phase

Backend build-prompt fragment when a datastore dependency or `persist_path` is declared: read
the connection URL from `conn_env` (or open the SQLite file at `persist_path`), create
schema/run migrations idempotently on boot, and persist there. PIN the DB driver version like
every other dep.

### 3.5 Verify (the 5b rework)

Today `verifier.py` (host) launches **one** M2 container that rebuilds from source and runs
`acceptance_runner.py` inside it. The rework, only when the scope contains a service target:

1. Host verifier creates a per-run docker network (reuse the `egress.py` network-create idiom).
2. For each service target: `docker run -d` its image on that network with a **named volume** at
   `volume_path` and its `env`; wait for `ready_cmd` to succeed (host-side `docker exec` poll).
3. Launch the existing rebuild+acceptance container **on the same network**, injecting each
   dependent backend's `conn_env` = `conn_url_template.format(host=<service network alias>,
   port=<in-container port>)`.
4. `acceptance_runner` boots the backend subprocess as before (preserving the in-container
   rebuild-from-source proof), now with `DATABASE_URL` pointing at the sibling datastore.
5. Tear down network + service containers after; the named volume is removed too (verify is
   ephemeral — the restart check below proves persistence *within* the run).

DB **image pulls happen via the host docker daemon** (host has internet), so they are *not*
gated by the `--internal` egress allowlist — only in-container HTTP egress is. No allowlist
change needed; document that the images must be pullable (or pre-pulled offline).

### 3.6 The `persistence_survives_restart` check (in `acceptance_runner`)

Runs against the booted backend:

1. POST to `route` with `body` → capture the created id at `json_path`.
2. **Restart the backend process**: `terminate()` the Popen, re-`Popen` the same `boot.cmd`,
   re-poll health. (The datastore container, if any, stays up across this.)
3. GET `verify_route` → assert the id from step 1 is present.

For SQLite this proves the file survived the app restart (the file lives in the target workdir,
which persists across the subprocess restart). For a managed service it proves state lives in
the datastore, not the app.

### 3.7 Deploy phase

`deploy.start_target` gains a **service branch** (mirrors the existing backend/frontend
branches): `docker run -d <image>` on a shared per-deploy network, named volume at
`volume_path`, env from `ServiceSpec.env`, wait for `ready_cmd`. `DeployPhase` ordering becomes
**datastores → backends → frontends**; backends join the network and receive `conn_env` =
resolved connection URL (network alias as host). Named volume → data survives `docker restart`.
`DeployGate` already probes every started target's health; service targets are health-gated by
`ready_cmd` at start (they expose no HTTP), so they are marked healthy on successful start
rather than HTTP-probed.

---

## 4. Components & isolation

| Unit | Responsibility | Depends on |
|---|---|---|
| `ServiceSpec` + `Recipe.kind` | Declarative datastore know-how | — |
| registry: `postgres`, `mongo` | Two engines the agent may pick | `ServiceSpec` |
| Scope prompt + `ScopeGate` | Let agent choose persistence; validate datastore refs | registry |
| build-prompt fragment | Datastore-aware backend scaffolding | scope detail |
| `persistence_survives_restart` | Engine-agnostic durability proof | acceptance_runner boot loop |
| verifier sibling-container bring-up | Source-verify the managed path | deploy service branch, egress net idiom |
| deploy service branch + ordering | Multi-container preview with a real DB | `ServiceSpec`, networks/volumes |
| `examples/fullstack-persistent.md` + live test | Exercises the whole seam | all of the above |

Each is independently testable: registry/schema by unit tests; the check by a fake HTTP server;
verifier/deploy service bring-up by a `DEVAGENT_RUN_LIVE`-gated live test.

---

## 5. The fixture

`examples/fullstack-persistent.md` = the M6 tasks app, plus one requirement line:

> **Tasks MUST persist across a server restart** (a task created via `POST /api/tasks` is still
> returned by `GET /api/tasks` after the API process is restarted).

Engine unspecified — the agent decides SQLite vs Postgres vs Mongo, same build vs a datastore
target. A `DEVAGENT_RUN_LIVE=1` test asserts the full pipeline reaches a green
`persistence_survives_restart` check and a deployed multi-container preview.

---

## 6. Risks / open questions

- **Verify wall-clock & cost** grow (image pull + DB boot + an app restart). Acceptable for a
  live-gated test; note it.
- **Which NoSQL.** Mongo is the registered representative; the seam is engine-general, so a
  second NoSQL is a registry entry, not a harness change.
- **Volume hygiene.** Verify removes its named volume each run (ephemeral); deploy keeps it so
  previews survive restart — the orphan-sweep that M6 added for containers must extend to these
  named volumes on teardown.
- **The agent might still pick SQLite for everything** because it's simplest. That's a correct
  outcome, not a bug — but the fixture/eval should include at least one PRD whose shape nudges
  toward a managed store (e.g. "multiple API instances share state") to exercise 3.5–3.7.

---

## 6b. Relationship to brownfield (M7/M8/M9)

This spec is **greenfield** — the agent *decides* an engine and scaffolds it. Brownfield (M9)
is the opposite: the agent *detects* the engine an existing repo already uses and builds with
its toolchain. The two do not merge into one spec (decide-vs-detect are different problems), but
this work is a **foundation M9 leans on**, not a conflict:

- `ServiceSpec` + the verifier's **sibling-container bring-up** (network, readiness poll,
  `conn_env` injection, named volume) is exactly what a cloned repo whose tests need a real DB
  requires. M9 reuses that machinery and only swaps the Scope-phase *decision* for stack
  *detection*.
- Persistence in brownfield is therefore *not* an agent choice — it's read from the repo. The
  "agent decides storage" framing here is inherently greenfield and stays scoped to greenfield.
- Pushing built output into an existing repo + its CI/deploy is **M7/M8**, orthogonal to how
  storage is verified. This spec touches neither.

## 7. Success criteria

1. The agent, given `fullstack-persistent.md`, autonomously chooses a persistence strategy and
   declares it in `ProjectScope` (SQLite or a datastore target) — no operator storage input.
2. `persistence_survives_restart` is a real machine-checkable check that fails for an in-memory
   store and passes for a persisted one.
3. When the agent picks a managed datastore, **verify** stands up a real sibling container and
   source-verifies the backend against it (not deploy-only).
4. Deploy yields a multi-container preview whose data survives `docker restart` of the app.
5. Unit suite green; the `DEVAGENT_RUN_LIVE` fixture green end-to-end.
