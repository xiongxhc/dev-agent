# dev-agent M7: Git Publishing — Design

**Milestone:** M7 (git-repo accretion — the number reserved since M6; `RepoBinding` stub in
`schema.py:144`). Depends on M20–M25 (built). Composes with M26 (production-bug capture,
separate spec, same date).
**Status:** Design approved (brainstorm 2026-07-15/16).

## Problem

Built systems live only in `runs/<feishu-run>/run-<id>/services/<name>/out/` on one host.
There is no durable, portable, versioned copy: the operator can't clone the code, an update
overwrites the prior build in place with no history, and a crashed disk loses every app the
team has built. The GitLab group token (created 2026-07-15: group access token, `api` scope,
Maintainer) exists precisely so every built app can live in the forge — per-service commits
during the build (durability/accretion, the original M7 intent) AND a clean final repo as the
deliverable the chat can link to.

## Decision summary

- **One built app = one GitLab monorepo**, created lazily on the first green service, named
  `<slug(design.title)>-<run-id-suffix>` in the configured group, private. Updates (M25) keep
  committing to the same repo on the default branch. A new repo happens only for a new
  build-system run (new run dir) — never for an update. Branch-per-update is deferred until
  asked for.
- **Export worktree, not run-dir-as-repo.** The publisher maintains a local clone at
  `run_dir/repo/` and **copies** green output into it (`services/<name>/out/` →
  `repo/services/<name>/`). The repo stays a clean deliverable (no `out/` nesting, no
  `ledger.jsonl` churn) and is decoupled from the live-preview mounts — same lesson as
  deploy-copies-not-mounts (2026-07 preview incident).
- **Both cadences.** Per-green-service commit+push during builds and repairs (accretion:
  a days-long M18-style build survives its host), plus a finalize commit (README,
  `.devagent/` metadata, full-tree sync) as the deliverable snapshot.
- **Integration is composition-only.** Everything hooks in at the `cli.py` layer by wrapping
  the injected `run_node` seam (the same trick `update_run_node` uses,
  `system_build.py:604`) plus a `finalize(report)` call after a `succeeded` return.
  `system_build.py`, `tree.py`, and the phase/gate stack are untouched.
- **No git/forge failure ever fails a green build.** Publishing is strictly additive.
- **Existing-repo binding (`mode="existing"`, user decision 2026-07-16).** A build request
  may name an existing repo on the configured forge; dev-agent then publishes into it on a
  **new branch created from `develop`** (fallback: the repo's default branch) — never onto
  the user's branches directly, never force-pushed. New-repo creation stays the default.

## Component

New module `devagent/gitops.py` — a `GitPublisher`:

- `GitPublisher.from_env(run_dir)` → publisher or `None`. Active only when ALL of
  `DEVAGENT_GITLAB_URL`, `DEVAGENT_GITLAB_TOKEN`, `DEVAGENT_GITLAB_GROUP` (id or path) are
  set. `None` ⇒ the CLI composes exactly today's pipeline; offline runs and the entire
  existing test suite are untouched.
- `wrap(run_node)` → a run_node that, when the inner builder returns `SUCCEEDED`, ensures
  the repo exists (first green service triggers `POST /projects` + `git init`/remote at
  `run_dir/repo/`, binding persisted to `run_dir/repo.json`: url, project path, default
  branch), copies that service's `out/` into `repo/services/<name>/` (excluding
  `node_modules`, `.git`, `__pycache__`, `.venv` — the executor-tar excludes), commits, and
  pushes. Commit messages carry context the wrapper already sees:
  `api: verified green`, `api: repaired (integration)` (repair_context set), or an
  update-prefixed form (below). In `update-system`, unchanged-node skips return before the
  inner builder (`system_build.py:607`), so they can never re-commit.
- `finalize(report)` → ensures the repo exists (covers an update that rebuilt zero services
  on a pre-M7 app, where the wrapper never fired), then full-tree sync **with delete** of
  `repo/services/` against
  `run_dir/services/*/out` (self-heals renames; mirrors `_reap_removed_services`), writes
  `README.md` (what the app is, per-service table with preview URLs from the report, how to
  run it) and `.devagent/` (`prd.md`, `design.json` — contracts included; on updates, appends
  the change request to `.devagent/change.md`), commits, pushes, and appends a
  `{"event": "repo", "url": ...}` ledger event.
- Update runs construct the publisher from the persisted `run_dir/repo.json`; a pre-M7 app
  receiving its first update simply creates its repo lazily then — same code path. Commit
  messages prefix the change request's first line:
  `update "add a count endpoint": api rebuilt green`.

## Existing-repo binding

- **Hand-over surface:** the Feishu build message may contain a repo URL; the bot extracts
  it only when its host matches `DEVAGENT_GITLAB_URL` (the token only works there anyway)
  and passes it to the CLI as `build-system --repo <url>`. CLI users pass `--repo` directly.
- **Branching:** the publisher clones the repo, bases itself on `develop` if it exists,
  else the remote default branch, and creates `devagent/<slug(title)>-<run-suffix>` from
  that base. ALL commits for this app's lifetime (per-green, finalize, every update) land
  on that one branch; the user merges into develop whenever they choose. The binding
  (`repo.json`) records `mode: existing`, `base`, and the branch as the push target.
- **Announcement:** the `repo` ledger event (and thus the chat's `📦 Code:` line) carries
  the branch **tree URL** (`<repo>/-/tree/devagent/...`), so the link lands on the app's
  branch, not the repo root.
- **Edge:** an empty existing repo (no commits) has no base to branch from — that's a
  `repo_error` (use the default new-repo mode instead). Updates never need the URL again;
  the binding persists in the run dir.

## Token & identity hygiene

Run dirs are browsable (the Feishu bot serves previews out of them), so **the token never
touches disk**: the remote URL in `.git/config` is credential-free; every push injects the
token via an ephemeral credential helper (`git -c credential.helper='!f(){ echo
username=oauth2; echo password=$DEVAGENT_GITLAB_TOKEN; };f'`). Commits are authored
`dev-agent <dev-agent@local>` via `-c user.name/-c user.email` (no global config writes).

## Chat surface (the feature is reachable end-to-end from Feishu)

- `_tail` (`channels/feishu_bot.py`) captures the `repo` ledger event the way it captures
  `system_deploy` urls; build/update done-messages gain one line: `📦 Code: <repo url>`.
- Per-commit events format to `None` in `_format_event` — no chat spam.
- If publishing failed, the done message says so once: built fine, not pushed, reason.

## Error handling

Every publisher call is exception-wrapped. A failure appends
`{"event": "repo_error", "detail": ...}` (surfaced once in chat) and the publisher goes
**dormant for the rest of the run** — no retry storms, no partial-state thrash. A rejected
push is reported, never force-pushed (single writer per run dir — the bot's per-chat lock —
so conflicts indicate something a human should see). The build/update report status is
computed entirely upstream of publishing and cannot be altered by it.

## Testing

House discipline — unit tests zero-network, zero-Docker, zero-token:

- Fake forge client (records `POST /projects`) + a local `file://` bare repo as the remote,
  so real `git` commit/push mechanics run in-tests.
- Covered: lazy repo creation on first green; unchanged-skips don't commit; repair commits;
  update prefix in messages; finalize delete-sync (a removed service vanishes from the
  repo); README/`.devagent` content; token absent from everything under `run_dir/repo/`
  (grep the clone); each failure mode (project-create 4xx, push rejected, git missing)
  leaves the run `succeeded` + one `repo_error` event + dormancy.
- Bot: done-message includes the URL; `repo`/`repo_error` event formatting.
- Live litmus (operator): one Feishu build + one update against the real GitLab group —
  expect per-service commits, a finalize commit, then update commits in the same repo.

## The repo is a one-way projection

The run dir remains the app's single source of truth; the repo is written, never read.
Updates (M25) already handle the "second run is brownfield" problem in the run dir itself —
the update executor edits the existing `services/<name>/out/` in place, guided by the prior
`design.json`/`.devagent` map — and the repo simply receives those commits on top of its
history. Consequence to document for users: commits pushed directly to the repo's
`services/` tree do NOT flow back into the app, and the next update's finalize delete-sync
replaces that content (the human commit survives only in history). Two-way sync
(repo-as-source-of-truth) is the future bridge to true brownfield ingestion, out of scope
here; the README generated into every repo states this one-way contract.

## Out of scope (each has a natural later home)

- Generated `docker-compose.yml` (deploy concern; bring-up doesn't use compose today).
- MR-based flows, branch-per-update ("update in a different branch if needed" — on ask).
- Checkpoint/resume from the repo (M18 — `.devagent/` + per-green commits are its anchor).
- Publishing single-service `run` builds (chat routes everything through build-system).
- GitLab issues for production bugs → **M26 spec** (depends on this one).
