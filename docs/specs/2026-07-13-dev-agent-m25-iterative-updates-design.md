# Dev-Agent: Iterative Updates (M25) — Design

**Date:** 2026-07-13
**Status:** Draft — research done, design pending approval
**Scope:** A follow-up chat message modifies the app already built in that chat, instead of
building a fresh one. Touches `devagent/channels/feishu_bot.py` (chat→app state + routing),
`devagent/system_build.py` (an `update_system` entry + selective rebuild + data-safe bring-up),
`devagent/phases/architect.py` (an update/diff mode), a new design loader, and
`devagent/sdk_runner.py` (an update prompt prefix). The per-service build pipeline
(`build_pipeline_phases`) needs **no change** — it already expresses the combination an update
needs.

## Problem

The chat is the product interface, but it is stateless. Every Feishu message does
`tempfile.mkdtemp(prefix="feishu-run-")` and shells `build-system <prd>` on that message's text
alone (`feishu_bot.py:125,133`); the handler keeps only a `seen_messages` dedupe set
(`feishu_bot.py:209-230`). So "add a dark mode" is read as an entire new requirement — it builds
a fresh, nonsensical app on new ports and never touches the app you just built. There is no
chat→run linkage, no design diff, and no way to rebuild only what changed. Conversational
refinement — the thing a chat interface implies — does not exist.

## The central decision: data continuity

Research verdict (the load-bearing finding): **preserving a running app's database across an
update is currently impossible**, for three independent reasons, and even fixing all three only
gets you *code* updates, not *schema* updates:

1. `start_service` unconditionally `docker volume rm`s the datastore volume on every bring-up
   (`deploy.py:128`).
2. `teardown` also removes the volume (`system_build.py:227`), and the repair loop tears down
   between passes (`system_build.py:405,438`).
3. The volume name is keyed to the per-run timestamp+UUID run dir
   (`devagent-sys-<run_dir.name>-<node>-data`), so two runs of the same app never share a volume.
4. Even with a stable, preserved volume: generated apps have **no migration runner**. They apply
   an inline `CREATE TABLE IF NOT EXISTS` blob on boot (`api/src/config/db.js` in the run dir),
   which is additive-only and cannot evolve a schema over existing rows. The `db/migrations/*.sql`
   files are dead (never read; already drifted — `date text` in the live schema vs `date date` in
   the migration file). The only convention is a soft prompt hint (`registry.py:43-56`), enforced
   by no gate.

**Recommended rule (drives the whole design): diff the `db_schema` contract, and let that decide
data fate.**

- **`db_schema` unchanged** (the common case — "change the button color", "add a dashboard
  chart", "fix the reject flow"): preserve the datastore volume. Data survives, because the
  generated `CREATE TABLE IF NOT EXISTS` is a safe no-op against the existing schema.
- **`db_schema` changed**: the generated app cannot migrate live data safely today. The update
  **resets the datastore** and says so explicitly in chat ("this change alters the data model —
  existing data will be cleared"). Data-preserving schema migration is a separate, larger
  milestone (a real ordered/tracked migration runner in generated apps + a gate that enforces
  additive-and-reversible migrations); it is **out of scope here** and called out below.

This is one capability with a principled fork on a fact we can compute (did the schema contract
change?), not a v1/v2 ladder. It ships the majority case (code updates preserve data) honestly
and refuses to fake the hard case.

## Decision summary

| Question | Decision |
|---|---|
| New build vs. update? | If the chat has a prior app AND the message reads as a modification → update. Explicit "start over" escapes to a fresh build. |
| Does the architect design from scratch? | No — update mode feeds it the prior `SystemDesign` + the change request; it emits a new `SystemDesign`. |
| Which services rebuild? | Only those whose `prd_slice`/contracts changed, plus consumers of a changed contract (from the design DAG). Unchanged services are reused in place. |
| How is one service updated? | Re-plan (live `PlanPhase`) against the new scope, build in the **existing** `out/` with the change request as context. |
| Change-set detection? | Mechanical diff of prior vs new `SystemDesign` (by service id and contract id+spec). Not an LLM emission — no schema change. |
| Data across an update? | Preserved when `db_schema` unchanged; reset (with a chat warning) when it changed. See above. |
| Preview URL across an update? | New ports today (ephemeral `_free_port`). Stable-URL is a separable nice-to-have, not required for v1. |

## Flow

```
Feishu message in chat C
  │
  ├─ no prior app for C, or message says "new app/start over"
  │     └─▶ existing build-system path (unchanged)
  │
  └─ prior app for C and message reads as a change
        └─▶ update_system(prior_run_dir, change_request):
              1. load prior design.json  → SystemDesign
              2. ArchitectPhase(update mode: prior_design + change) → new SystemDesign  [gated]
              3. diff(prior, new) → changed service ids (+ consumers of changed contracts)
                 and a schema_changed flag (db_schema contract diff)
              4. for each changed service: _update_build_service (re-plan + build in existing out/)
                 reuse unchanged services' prior out/ as-is
              5. bring_up(new design, preserve_data = not schema_changed)
              6. integration + security reverify  →  M23 repair loop (unchanged)
              7. redeploy; post preview URL(s) + a note if data was reset
```

## Components

### 1. Chat→app state (`feishu_bot.py`)
Persist a map `chat_id → latest run_dir` to disk (survives bot restart) — e.g. a small JSON under
`_RUNS_BASE`. On each message, look up the chat's prior run. Serialize per chat (a lock) so two
messages can't race the same app. The bot stops using a throwaway `mkdtemp` for updates: an update
reuses the prior run dir (its `services/<name>/out` and `design.json` are the state).

### 2. Intent routing (`feishu_bot.py`)
Decide new-vs-update. Cheapest reliable option: if the chat has a prior app, treat the message as
an update unless it matches an explicit new-app escape ("new app", "start over", "build me a…").
An LLM classifier is an option but adds a call; start with the escape-word convention and iterate.

### 3. Architect update mode (`phases/architect.py`)
Add optional `prior_design: SystemDesign | None` and `change_request: str | None` to
`ArchitectPhase`. When set, use an update-mode prompt that includes the serialized prior design +
the change and emits a full `SystemDesign` (same output type — `generate_structured(..,
SystemDesign)` is unchanged). **Pin names:** the prompt must instruct the LLM to keep unchanged
services' `id`/`name` identical (per-service dirs and containers key on `node.name`,
`system_build.py:128,247`; a silent rename orphans the prior `out/`). The `ArchitectGate` is
stateless and already accepts reused names — no gate change needed, but name-stability of
*unchanged* services should be asserted post-emit.

### 4. Design loader (new, tiny)
`design.json` is written (`system_build.py:354-359`) but never reloaded in prod. Add
`SystemDesign.model_validate_json(Path(run_dir)/"design.json").read_text())` (the pattern already
exists for `Plan` at `system_build.py:182`). Validators re-run on load — desirable.

### 5. Selective rebuild (`system_build.py`)
New `_update_build_service(node, design, svc_dir, budget, ledger, change_request)` — a sibling of
`_real_build_service` (`system_build.py:146`). Same setup, but `plan=None` (so `PlanPhase`
re-plans against the new scope) and the change request passed as build context. Everything else is
identical; `build_pipeline_phases` already supports FrozenScope + live PlanPhase + BuildPhase-with-
context at an existing `out_dir` (the params are orthogonal — the only reason `_real_build_service`
can't do this is that it couples "context set ⟹ reload frozen plan", `system_build.py:180-182`).
A new `update_system` orchestration entry loads the prior design, diffs, calls
`_update_build_service` for changed nodes, leaves unchanged nodes' `out/` untouched, then reuses
the existing bring-up + M23 reverify loop. Scope is re-derived from the design each build
(`scope_for_node`), so the new design's contracts/checks flow through automatically.

### 6. Update prompt prefix (`sdk_runner.py`)
The only "edit in place, don't start over" framing today is `REPAIR_PREFIX` (`sdk_runner.py:21-30`),
which tells the model "a previous attempt … FAILED". Feeding a feature-add through it mislabels the
work. Add an `UPDATE_PREFIX` ("the app is already built in /out; apply this change, edit in place,
keep the lockfile valid") selected when the context is an update rather than a repair. Note: any
context set already forces the sequential whole-project session (`executor_sdk.py:77`) — correct
for coherent editing.

### 7. Data-safe bring-up (`system_build.py`, `deploy.py`)
Add a `preserve_data` path through bring-up/teardown:
- Volume name derived from a **stable app id** (the chat's app slug), not `run_dir.name`.
- In preserve mode, `start_service` skips its `docker volume rm` (`deploy.py:128`) and teardown
  skips `docker volume rm` (`system_build.py:227`), so the datastore keeps its volume across the
  update's teardown/bring-up.
- `update_system` passes `preserve_data = not schema_changed`. When `schema_changed`, it takes the
  normal wipe path and the bot warns the user.

## Known edges / risks

- **Node rename across an update** orphans the prior `out/` and its data. Mitigated by name-pinning
  (Component 3) + a post-emit assertion; a rename should be treated as a new service (rebuild) not
  an in-place edit.
- **Weakly-verified features.** Acceptance/integration checks derive from contracts
  (`contract_utils.py`); an update whose new behavior isn't expressible as a contract/check
  (e.g. a pure styling change) passes verify without being exercised. Acceptable, but note it.
- **Contract versioning.** `Contract.version` is frozen at 1 (`schema.py:185-193`); there's no
  renegotiation ledger. Consumers rebuild against the new spec, but nothing tracks a contract as
  "changed" for you — reinforcing that the change-set is a diff we compute, not state we read.
- **Re-plan may re-partition files** differently from what's on disk; stale files from the old
  plan aren't auto-deleted. The whole-project sequential session reconciles at edit time, but this
  is a real source of drift on large changes.
- **Preview URL churn** — ports are ephemeral (`deploy.py:42-47`). Stable-URL-across-update is a
  separate change (pin the host port to the app slug); not required for v1.

## Testing

- Architect update mode: prior design + change → new design; unchanged services keep id/name;
  a contract-shape change shows up in the diff.
- Design diff: code-only change → zero db_schema delta → `preserve_data=True`; a table/column
  change → schema delta → `preserve_data=False`.
- Selective rebuild: only changed nodes re-run; unchanged nodes' `out/` untouched (mtime/inode).
- Data-safe bring-up (docker-marked): create a row, run a code-only update, assert the row
  survives; run a schema-changing update, assert the reset + the chat warning.
- Bot routing: prior app + "add X" → update path; "start over" → fresh build; two rapid messages
  serialize.

## Out of scope (explicitly deferred)

- **Data-preserving schema migrations** — the real prize and the genuinely hard part. Requires a
  generated-app migration runner (ordered, tracked in a `schema_migrations` table, additive) plus
  a dev-agent gate that enforces additive-and-reversible migrations, plus a destructive-change
  policy. This is its own milestone; M25 deliberately resets data on schema change instead of
  faking it.
- **Stable preview URLs** across updates (port pinning).
- **Multi-turn conversational memory** beyond "the last design in this chat" — M25 carries state,
  not history.
