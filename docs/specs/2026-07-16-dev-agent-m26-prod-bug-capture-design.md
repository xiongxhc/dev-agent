# dev-agent M26: Production-Bug Capture → GitLab Issues → Confirm-First Fix — Design

**Milestone:** M26. Depends on **M7 (git publishing)** — issues live in the app's repo and
evidence cites the running commit SHA. Builds on M23 (repair loop) / M25 (update-system),
which are the fix machinery; M26 adds no new fixer.
**Status:** Design approved (brainstorm 2026-07-16). Fix policy: **confirm-first** (user
decision) — the monitor never spends tokens or redeploys unprompted.

## Problem

Once a built app is live (preview containers), nobody is watching it. A service that crashes
overnight, starts 500-ing, or restart-loops is discovered only when a user happens to hit it
in chat, and the evidence (logs, dying container state) may be gone by then. Bugs need to be
**captured with evidence at detection time, deduplicated, and filed where the code lives**
(the M7 repo's issue tracker), with the existing M25 update loop as the one-command fix path.

## Decision summary

- **A preview monitor in the bot process** watches every chat-bound app (the
  `chat_apps.json` bindings). No new daemon: a watcher thread with a per-app cursor,
  polling on a lazy interval (`DEVAGENT_MONITOR_INTERVAL_S`, default 60).
- **Three machine signals + one human signal:**
  1. container state — exited / restart-looping (`docker inspect` restart count delta);
  2. error logs — `docker logs --since <cursor>`, traceback/ERROR patterns;
  3. health probes — periodic GET on each service's health path (known at deploy time);
  4. chat reports — a `bug: ...` message in a bound chat files an issue verbatim (the
     user's words ARE the intent statement).
- **Fingerprint before filing.** `hash(service, normalized error signature)` → issue label
  `devagent-fp-<hash>`. The monitor searches the project's open issues by label first: a
  recurring crash bumps a comment/occurrence count on the existing issue, never a duplicate.
- **Confirm-first fixing.** Every capture is issue + one chat line. A fix runs only on an
  explicit `fix issue N` in the chat. Unprompted auto-fix (even for deterministic classes)
  is deferred to a later opt-in flag.

## Autonomy boundary — which issues the fix path may claim

The boundary is **verifiability, not difficulty** (the M24 principle: only deterministic
probes may gate). `fix issue N` is honest only when the pipeline can mechanically confirm
the bug is gone:

**Fixable via `fix issue N`** (deterministic repro → checkable after the update):
- crash loops / container exits with a stack trace — blamed service rebuilt; verified by
  container-stays-up + probes green;
- reproducible 5xx on an endpoint — the failing request becomes a check (repair-loop bread
  and butter);
- cross-service contract violations — integration checks already derive from contracts;
- advisory (non-gating) M24 security findings shipped with the app — probes re-verify;
- regressions caught by existing acceptance checks;
- user-reported wrong behavior — the report is the new intent; it drives a normal M25
  update.

**Not claimable — filed, labeled `needs-human`, never auto-fixed:**
- intermittent / load- or data-dependent failures (no deterministic repro ⇒ an attempted
  fix is an unfalsifiable claim and pure token burn);
- anything requiring destructive data operations on live data (the preserve-mode concern);
- host/infra problems (ports, docker daemon, disk) — ops, not code.

The monitor classifies at capture time (repro-able signal vs not) and labels accordingly;
`fix issue N` on a `needs-human` issue answers in chat why it won't run.

## Mechanics

- **Preview metadata.** Deploy currently keeps urls in the report but not container names /
  health paths in a monitor-readable place. Small addition at bring-up success: write
  `run_dir/preview.json` (service → container name, url, health path). The monitor reads
  only this + docker + the forge.
- **Cursor state** at `run_dir/monitor.json` (log timestamps, restart counts, probe
  status, fingerprints already filed) — survives bot restarts, no re-filing storms.
- **Issue content:** service, evidence block (log tail / probe result / inspect excerpt),
  running commit SHA + repo path (from M7's `repo.json`), run id, labels
  (`production-bug`, `auto-captured`, `devagent-fp-<hash>`, class label).
- **Chat notice** (one line per new issue): `🐛 Captured a crash in api → issue #12 <url>.
  Say "fix issue 12" and I'll fix and redeploy.`
- **Fix flow:** `fix issue N` → fetch issue → synthesize `change.md` (title + evidence +
  "behavior must remain per .devagent/design.json contracts") → the normal M25
  `update-system` stream into the same chat. On success the update's commits append
  `Fixes #N` (GitLab auto-closes on default-branch push) and the issue gets a comment with
  the verification evidence; on failure the issue stays open with a failure comment and the
  chat says so. No retry loop.
- **Routing:** `bug:`/`fix issue N` intents slot into the bot's `_route` before the build
  fallback — same pattern as the help card (and its chained-intent lesson: pattern-match
  liberally, never let a bug report trigger a fresh build).

## Error handling

The monitor must never hurt the thing it watches or the bot that hosts it: every poll is
exception-wrapped; forge-down ⇒ skip the cycle (cursor holds, capture retried next tick);
docker-gone ⇒ mark the app dormant and note it once in chat. Monitor failures are ledgered
(`monitor_error`), never raised. Probes are GETs on health paths only — the monitor never
mutates the app (M24 lesson: probing can dirty a live system).

## Testing

- Unit, zero-network/zero-Docker: fake docker (inspect/logs fixtures) + fake forge —
  detection per signal class; fingerprint stability across log noise (timestamps, ids
  normalized out); dedup (second capture comments, doesn't re-file); classification
  (`needs-human` vs fixable); cursor persistence across restarts; `fix issue N` synthesizes
  the right change.md and refuses `needs-human`; monitor exceptions never propagate.
- Bot: routing (`bug:` files, `fix issue N` triggers, neither ever falls through to a
  build); chat notice formatting.
- Live litmus (operator): kill a preview container's process → issue appears with evidence;
  `fix issue N` → update runs, issue auto-closes, preview healthy.

## Out of scope

- Unprompted auto-fix (future opt-in flag per app, deterministic classes only).
- Metrics/latency monitoring, dashboards, alert escalation policies.
- Heuristic wrong-behavior detection (intent belongs to users; chat reports cover it).
- Monitoring apps not bound to a chat (nothing else is live today).
