# dev-agent — System-Build Live Path (datastores, cross-service wiring, contracts)

**Date:** 2026-07-02
**Status:** Design.
**Milestone:** M21 (system-build live path). Depends on M20 (built). Closes the five design-scale findings of the M20 whole-branch review.

## 1. Goal

Make `devagent build-system` **survive a real multi-service PRD**. M20 wired the seams; the whole-branch review confirmed the live path still fails on virtually any db-backed or multi-service system: datastore nodes are LLM-built (and can nondeterministically sink the run), brought-up services can't reach each other (no conn env, stale frontend config), bring-up mounts directories the sub-build may never have written, sub-builds never see the contracts they consume, and every sub-run deploys colliding preview containers it never tears down.

Success: on a `db → api → web` PRD, every node builds or comes up by the mechanism appropriate to it, the api container boots with a working `DATABASE_URL`, the web bundle points at the live api, the integration E2E passes, and no orphaned containers remain.

## 2. Scope

**In:** the five fixes below — all inside `system_build.py`, `cli.py`, `phases/deploy.py` (one extraction), `executor.py`/`executor_sdk.py`/`managed_executor.py`/`phases/build.py` (one threaded field). Unit-tested with fakes; no Docker/tokens in tests.

**Out (deferred):** backend→backend runtime addressing (no env convention exists for a built service to read a peer's URL — needs a contract-level convention first, see §8); per-run-unique preview container names in `deploy.py` (fixed names are how single-run re-deploys replace old previews; concurrent *system* runs remain exclusive); contract conformance probes at system bring-up; M7/M18/M19 unchanged.

## 3. Fix 1 — datastore nodes come up from recipes, never LLM builds

**Problem:** `make_run_node` pushes every node through the full scope→plan→build→verify sub-run (`system_build.py:20-28` — no kind dispatch); a `postgres` node burns budget on a nondeterministic "build" of something `bring_up` recreates from the recipe image anyway, and its failure blocks dependents.

**Decision:** in `run_node`, if the node's recipe is service-kind — the canonical idiom `recipes.get(node.stack).kind == "service"` (used at `phase_gates.py:153,167`, `phases/deploy.py:44`, `verifier.py:96,150,190`) — return `NodeResult(node.id, SUCCEEDED, "service node: no build; started from recipe image at bring-up")` without calling `build_service`. Also replace `make_bring_up`'s non-canonical `node.kind in ("datastore", "service")` (`system_build.py:105`, the `"service"` literal is unreachable — no recipe has `type=="service"`) with the same recipe-kind idiom. ArchitectGate already guarantees `node.stack` is registered (`phase_gates.py:207-209`), so the lookup cannot KeyError post-gate.

## 4. Fix 2 — sub-runs stop deploying previews (`deploy=False`)

**Problem:** `_real_build_service` passes `build=True`, and `build_pipeline_phases` unconditionally appends `DeployPhase`+`DeployGate` (`cli.py:44-52`). Each sub-run deploys `devagent-preview-<target.name>` on the shared `devagent-preview-net`; concurrent siblings `docker rm -f` each other's previews mid-gate (nondeterministic build failures), the previews (`--restart unless-stopped`) leak, and the frontend `dist/config.json` gets written pointing at a preview that is then dead.

**Decision:** add `deploy=True` keyword to `build_pipeline_phases`, gating only the `DeployPhase`/`DeployGate` append; `_real_build_service` passes `deploy=False`. Verified safe: `Orchestrator` matches gates by name with no hardcoded phase list (`orchestrator.py:38-56`); `BuildVerifier` runs its own `--rm` containers and `devagent-verify-<run_id>-*` datastores, fully independent of previews (`verifier.py:101-110,164,252-263`); only `main`'s own `run --build` path reads the deploy artifact (`cli.py:189-194`) and it keeps the default. This also removes the sub-run's stale `config.json` write — bring-up (Fix 5) becomes the single writer.

## 5. Fix 3 — consumed contracts reach the sub-build prompt

**Problem:** the M16 seam exists (`enrich_scope(scope, consumed_by_target)` → `_consumed_contracts` → "CONSUMED CONTRACTS" prompt block, `executor.py:56-81`, `sdk_runner.py:82-86`) but nothing live passes it: both executors call `enrich_scope(req.scope)` bare (`executor_sdk.py:56,83`, `managed_executor.py:68`), `BuildRequest` has no field for it, and `build_pipeline_phases` has no parameter. Every sub-build invents its own client shapes; integration then fails on the mismatch M16 was built to prevent.

**Decision:** thread a **service-level** list, broadcast to every target:

- `BuildRequest` gains `consumed_contracts: tuple = ()` (frozen dataclass, `executor.py:20-27`).
- `BuildPhase(..., consumed_contracts=())` stores and stamps it on each `BuildRequest` it constructs (`phases/build.py:36-43` and the repair path).
- `build_pipeline_phases(..., consumed_contracts=())` passes it to `BuildPhase`.
- Both executors call `enrich_scope(req.scope, consumed_by_target={t.name: list(req.consumed_contracts) for t in req.scope.targets})` when the tuple is non-empty (and the per-target sub-scope path `executor_sdk.py:83` likewise). Broadcasting per-target sidesteps the keying trap the research confirmed: `enrich_scope` keys by the LLM-chosen scope target name (`executor.py:81`), which is *not* pinned to `node.name` — and since a sub-run builds exactly one service, every target of that sub-run legitimately builds against the service's consumed contracts.
- `run_node` computes `[c.spec for c in contracts_for_node(node, design)]` (`contract_utils.py:9-13` — it already holds both `node` and `design`) and passes it through `build_service`, whose signature becomes `build_service(node, design, svc_dir, budget, ledger)`; `_real_build_service` forwards it to `build_pipeline_phases`.

Alternative rejected: pinning scope target names to `node.name` via the prompt (LLM-fragile, needs a new gate) — bring-up instead adapts to the names actually built (Fix 4).

## 6. Fix 4 — bring-up mounts what was actually built (scope.json, not `node.name`)

**Problem:** `bring_up` mounts `services/<node.name>/out/<node.name>` (`system_build.py:104,110` → `deploy.py:155,178`), but the sub-run's ScopePhase freely names targets (`phases/scope.py:25`) and may emit several. A mismatch mounts an empty dir; docker still returns a URL; integration fails on a green build.

**Decision:** after a node's sub-build succeeds, bring-up reads the sub-run's persisted enriched scope at `services/<node.name>/out/.devagent/scope.json` — the exact idiom `BuildVerifier` already uses (`verifier.py:85-87`); `SdkExecutor` always writes it (`executor_sdk.py:53-56`), including per-target `detail` and recipe `kind`. Bring-up then drives the node's **actual targets** (Fix 5) instead of assuming one target named `node.name`. A missing/unreadable scope.json ⇒ the node's services simply don't start ⇒ absent from `base_urls` ⇒ M17 fails its steps cleanly (established semantics).

## 7. Fix 5 — cross-service wiring at bring-up (extract and reuse DeployPhase's loop)

**Problem:** `bring_up` starts backends with `env=None` (no `DATABASE_URL`) and frontends with whatever stale `config.json` exists; `DeployPhase` already solves intra-scope wiring — datastores first, `conn_env = svc.conn_url_template.format(host=ds, port=svc.port)` from `target.detail.datastore`/`detail.conn_env` (`phases/deploy.py:44-97`), frontend `dist/config.json = {"apiBase": <first backend url>}` — but it's all inlined in `DeployPhase.run` and unreachable from system bring-up.

**Decision:** extract that loop into a reusable function in `phases/deploy.py`:

```
wire_targets(targets, out_dir, network, *, alias_prefix="", extra_env=None,
             start_service=deploy.start_service, start_target=deploy.start_target)
    -> (urls: dict[name -> url], services: dict[name -> container])
```

- **DeployPhase** calls it with `alias_prefix=""`, `extra_env=None` — behavior byte-identical (pure refactor, existing `tests/test_deploy.py` pins it).
- **bring_up**, per node in `topo_order`, loads the node's scope.json targets (Fix 6→4) and calls it with `network=<per-run net>`, `alias_prefix=f"{node.name}-"` — namespacing the *intra-node* datastore aliases/containers so two nodes' internal `db` targets can't collide on the shared run network (the prefix applies to both the `--network-alias` and the conn-URL `host`, which must match), and:
  - `extra_env`: for each design-level dependency `D` of the node with a service-kind recipe (started un-prefixed as a design node, alias `D.name`), inject `{"DATABASE_URL": recipes.get(D.stack).service.conn_url_template.format(host=D.name, port=svc.port)}` — the established conn-env convention (`phases/scope.py:44-48`, default per `verifier.py:192-213`). Intra-node scope wiring, when present, overrides it (the target's own `detail.conn_env` is more specific).
  - cross-node frontend apiBase: if the node's scope has frontend targets but no internal backend, `wire_targets` accepts `frontend_api_base=<base_url of the node's first backend dependency>` so the web node's `config.json` points at the live api node.
- The node's `base_urls[node.id]` = the wired targets' primary URL (frontend preferred, else first backend — DeployPhase's existing rule, `phases/deploy.py:104-108`). Container names collected into the existing `started` list so teardown removes them (M20 semantics preserved).

Design-level service nodes (Fix 1's, e.g. a shared `db` node) keep starting via `start_service(node, network=net)` with alias `node.name`, un-prefixed — that's what Fix 5's `extra_env` host points at.

## 8. Deferred (documented, not gaps)

- **Backend→backend addressing:** a built api has no convention to read a peer's URL from env; contracts describe interfaces, not addresses. Needs a design (likely `enrich_scope` teaching the build "peer `X` is at env `X_URL`" + bring-up injecting it). Until then, multi-backend systems integrate only through the host-side E2E.
- **Concurrent system runs:** bring-up container names remain in the global `devagent-preview-*` namespace (deploy.py's replace-on-redeploy semantics). One system run at a time.
- **Conformance probes at bring-up** (ContractConformanceGate over live services); M7 git accretion; M18 checkpoint/resume; M19 contract evolution.

## 9. Reconciliation

- Fixes 1–5 are exactly the five CONFIRMED design-scale findings of the M20 whole-branch review (29-agent workflow, 2026-07-02), at the altitude the review recommended (recipe-kind dispatch; deploy-less sub-runs; scope.json as the source of built truth; broadcast contract injection; DeployPhase-logic reuse).
- Reuses established idioms only: recipe-kind detection, `out/.devagent/scope.json` recovery (verifier), `conn_url_template` + `DATABASE_URL` convention (DeployPhase/scope prompt), `enrich_scope`/M16, injectable callables throughout. The only structural change to existing code is the `wire_targets` extraction (a refactor pinned by existing tests) and one additive field on `BuildRequest`/`BuildPhase`/`build_pipeline_phases`.
- M14–M17 modules remain untouched.
