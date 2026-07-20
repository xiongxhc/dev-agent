#!/bin/bash
# deploy-bot.sh — deploy the Feishu bot from master to ~/apps/team-bot and (re)start it.
# The bot never runs from a dev checkout: src/ is a disposable copy of master;
# .venv/ and runs/ persist across deploys (live previews bind-mount run dirs).
set -euo pipefail

DEPLOY="$HOME/.local/share/local-agent-team/bot"
ENV_FILE="$HOME/.config/local-agent-team/dev-agent.env"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

mkdir -p "$DEPLOY/runs"
rm -rf "$DEPLOY/src"
mkdir -p "$DEPLOY/src"
git -C "$REPO" archive master | tar -x -C "$DEPLOY/src"

[ -x "$DEPLOY/.venv/bin/python" ] || python3.12 -m venv "$DEPLOY/.venv"
"$DEPLOY/.venv/bin/pip" install -q -e "$DEPLOY/src/dev-agent[feishu]"

pkill -f 'devagent.channels.feishu_bot' 2>/dev/null || true
sleep 1
set -a; source "$ENV_FILE"; set +a
export DEVAGENT_RUNS_DIR="$DEPLOY/runs"
cd "$DEPLOY/src/dev-agent"
nohup "$DEPLOY/.venv/bin/python" -m devagent.channels.feishu_bot >> "$DEPLOY/feishu_bot.log" 2>&1 &
echo "team-bot deployed from master ($(git -C "$REPO" rev-parse --short master)) — pid $!"
