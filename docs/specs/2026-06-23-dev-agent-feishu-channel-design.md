# DevAgent — Feishu Group-Bot Channel (Design Note)

**Date:** 2026-06-23
**Status:** Design captured — build AFTER M3 (deploy), so the first trigger returns a real preview URL.
**Relation to core:** a **channel adapter**, not a core change. The pipeline stays unaware of Feishu (mirrors the repo's `core/` vs `adapters/` split).

---

## Vision (user's words)
A Feishu **group chat** where **everyone in the group can trigger and talk to the AI**:
drop a PRD (PDF or a plain message) → an autonomous build run happens → the result
(preview URL + screenshot + report) posts back into the group; and members can also
*converse* with the AI — ask status, follow up, steer.

## Two interaction modes through one bot
1. **Trigger** — "build X" / a PRD PDF → `enqueue_run(...)` → autonomous pipeline.
2. **Converse** — "how's the build?", "make it dark mode", general Q&A → `converse(...)`
   (a Claude chat with tools: list runs, get status, fetch report, trigger a run).

```
Feishu group ──@bot · file · message──▶ Feishu adapter (event subscription)
                                            │ intent classify
                              ┌─────────────┴─────────────┐
                        trigger                       converse
               enqueue_run(requirement,         converse(text, requester,
                 requester_id, reply_target)       thread) → tool-using chat
                              │                         │
                     run (own container)        reads run state / triggers
                              ▼                         ▼
                   reply IN-THREAD: URL + screenshot + report
```

## The seam the core must expose (design now, so the adapter drops in later)
- `enqueue_run(requirement: str | PdfRef, requester_id: str, reply_target: ThreadRef) -> run_id`
  — channel-agnostic; CLI and Feishu both call it.
- `converse(text, requester_id, thread) -> reply` — a separate conversational path.
- A **run registry** (list/status/report by run_id) the converse path queries. The ledger
  already gives per-run state; the registry is a thin index over `runs/`.

## New concerns a SHARED, multi-user trigger raises (decide early)
1. **Cost / abuse control (biggest)** — "everyone can trigger" + pay-per-token + real
   infra per run = a shared blank check. Need: per-user / per-group **rate limits**, a
   **budget cap**, optionally an **allowlist** or a **one-tap confirm** before an
   expensive run. Surfaces token spend per run back to the group.
2. **Concurrency** — many users, many simultaneous runs. Each run is already an isolated
   disposable container with its own id; add a **run queue** + **in-thread replies** so
   the group isn't chaos.
3. **Identity / attribution** — tag each run with the Feishu requester; route status and
   replies to the right person/thread.

## What it needs to build (later)
- **Feishu adapter**: event subscription (group message / @mention / file), file
  download, send-message-back. NOTE: this repo already has a `feishu-journal-obsidian`
  skill that reads Feishu — a Feishu app + credentials likely already exist; verify the
  app's **scopes** (receive file messages, download files, send messages, group events).
- **PDF → text** intake (a PRD PDF → text → Brief): `pymupdf`/`pdfplumber`. The current
  `intake` phase reads a text file; add a PDF-aware variant.
- The **conversational handler** (Messages API chat with run-registry tools).
- **Rate-limit / budget / allowlist** middleware on `enqueue_run`.

## Sequencing
Build Executor → verify → **deploy (M3)** → Feishu adapter. Don't build Feishu before
there's a deployed artifact to return. But the `enqueue_run` / `converse` seam + the run
registry should be shaped now so the adapter is a clean drop-in.
