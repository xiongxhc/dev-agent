# Examples

Requirement docs you can hand to dev-agent as-is. They are deliberately written the way a
stakeholder would write them — what the app must do, never how (no endpoints, stacks, or
env vars: designing those is dev-agent's job).

| Example | What it asks for | What it exercises | How to run |
|---|---|---|---|
| [`hello.md`](hello.md) | One greeting page | Smoke test — cheapest full pipeline pass | Feishu or `run --build` |
| [`tasks.md`](tasks.md) | Shared team task list | Fullstack + real shared datastore + persistence | Feishu or `run --build` |
| [`notes-auth.md`](notes-auth.md) | Private notes with login + admin | Auth (sessions, ownership, roles) + persistence | Feishu or `run --build` |
| [`system-polls.md`](system-polls.md) | Team polls product | **Multi-service system build** (M20/M21): Architect → per-service builds → wired bring-up → cross-service E2E | `build-system` (CLI only) |

**Feishu:** paste the example's text as a message to the bot (DM or @mention) — the message
body *is* the PRD; the bot streams phase progress back and posts the preview URL.
The Feishu channel currently triggers single runs (`run --build`); multi-service system
builds (`system-polls.md`) run via the CLI: `python -m devagent.cli build-system examples/system-polls.md`.

`corpus.json` is the M5 eval manifest — it pins which of these fixtures the A/B eval
replays and how many runs per arm. It is not an example.
