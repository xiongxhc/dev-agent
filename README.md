# dev-agent

Headless autonomous web-app builder. **Status: Milestone 1 — skeleton + sandbox.**

A deterministic Python harness drives bounded Claude phases (LLM brain, deterministic
hands). M1 builds the harness skeleton and the disposable, hardened container sandbox —
and proves containment — **before any token is spent** (the only phase so far is a
no-op shell command). See the design + research under
`../docs/superpowers/specs/2026-06-22-dev-agent-research-synthesis.md`.

## Run

```bash
python -m devagent.cli run examples/hello.md
# -> "run-<ts>-<id> succeeded"   (provisions a --rm container, runs the no-op phase,
#    gate passes, records the run to runs/<id>/ledger.jsonl, destroys the container)
```

Requires a running Docker daemon. Config via env: `DEVAGENT_IMAGE`, `DEVAGENT_RUNS_DIR`,
`DEVAGENT_MAX_TOKENS`, `DEVAGENT_MAX_SECONDS`, `DEVAGENT_MAX_RETRIES`.

## Test

```bash
.venv/bin/python -m pytest -q            # unit suite (no Docker)
.venv/bin/python -m pytest -q -m docker  # containment + e2e (needs Docker)
```

## Sandbox model (M1)

Each run gets a disposable `docker run --rm` container, **network-closed by default**
(`--network none`) and hardened per Anthropic's secure-deployment guidance:
`--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, non-root `--user`,
pid/mem/cpu limits. The run's `out/` directory is the **only** writable host mount. The
exact `docker run` argv + image digest are recorded to the ledger for auditability.

## M2 TODOs (carried from research)

- Real `intake`/`spec`/`plan`/`build`/`verify` phases on the Claude Agent SDK
  (`SdkExecutor`) — `permission_mode="dontAsk"` + explicit `allowed_tools` (NOT
  `bypassPermissions`; subagents inherit it and can't override).
- Relax `--network none` to an **out-of-sandbox proxy allowlist** (registries +
  api.anthropic.com + deploy target).
- Credential **proxy-injection** — the brain never sees deploy/API tokens.
- Build subagents must NOT include `Agent`/`Task` in their tools (prevents nested-spawn
  recursion); parent must include `Agent` to fan out.
- Real build toolchain in the sandbox image (Node/pnpm, Playwright).
- A second `ManagedExecutor` (Claude Managed Agents) behind the same `Executor` seam,
  then the eval corpus + A/B run.
