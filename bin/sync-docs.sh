#!/bin/bash
# sync-docs.sh — refresh the vendored design docs under docs/ from the monorepo's
# planning tree. Specs are AUTHORED in ../docs/planning/dev-agent/specs (the monorepo
# keeps one planning tree for all agents); the copies under docs/ exist so the
# standalone GitLab mirror is self-contained. Run this before a `git subtree push`.
# No-op outside the monorepo (the mirror has no ../docs).
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="../docs/planning/dev-agent/specs"
[ -d "$SRC" ] || { echo "not in the monorepo ($SRC missing) — nothing to sync"; exit 0; }
mkdir -p docs/specs
cp "$SRC"/*.md docs/specs/
cp ../docs/how-dev-agent-works.html docs/
echo "synced $(ls docs/specs/*.md | wc -l | tr -d ' ') specs + how-dev-agent-works.html"
