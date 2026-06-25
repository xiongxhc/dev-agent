"""Runs INSIDE the M2 sandbox container. Drives the Claude Agent SDK to build the app
per the frozen Scope+Plan (written by the host to /out/.devagent/), writing the app to
/out. Reports usage + any error to /out/.devagent/result.json.

setting_sources=[] is mandatory (proven in de-risk) so the in-container SDK never tries
to load host ~/.claude settings/hooks. Network egress (api.anthropic.com + npm) is
controlled by the host's `docker run` flags, not here."""

import argparse
import json
import traceback
from pathlib import Path

# claude_agent_sdk + anyio are imported lazily inside run()/__main__ so this module — and
# its pure token-accounting helper — import on the host (which lacks the SDK) for unit tests.

OUT = Path("/out")
DEV = OUT / ".devagent"

REPAIR_PREFIX = """\
This is a REPAIR pass. A previous attempt was rebuilt from source and FAILED with the \
diagnostics below. The existing files are already in /out — fix the specific failures, \
do not start over. Keep the lockfile valid (`pnpm install --frozen-lockfile` must pass).

BUILD FAILURE DIAGNOSTICS:
{diagnostics}

---
"""


def input_output_tokens(usage) -> tuple[int, int]:
    """(input, output) totals from a cumulative SDK usage dict. Input INCLUDES cache
    create + read tokens — for the trivial probe these dominated (15k cache-read vs 2k
    fresh input), so dropping them under-counts by ~7x and is the bug this fixes."""
    if not isinstance(usage, dict):
        return 0, 0
    tin = ((usage.get("input_tokens") or 0)
           + (usage.get("cache_creation_input_tokens") or 0)
           + (usage.get("cache_read_input_tokens") or 0))
    return tin, (usage.get("output_tokens") or 0)


def build_prompt(scope: dict, plan: dict) -> str:
    """Pure, host-importable prompt builder — consumes baked scope dict (registry-free)."""
    lines = [f"You are building '{scope['title']}'. Write ALL files under /out. Never write outside /out.\n"]
    for t in scope["targets"]:
        lines.append(
            f"Build target '{t['name']}' (type={t['type']}, stack={t['stack']}) "
            f"in directory ./{t['name']}/:"
        )
        lines.append(t["_scaffold_hint"])
        lines.append(f"Target detail (JSON): {json.dumps(t['detail'], indent=2)}")
        lines.append(
            f"Run `{t['build_cmd']}` in ./{t['name']} until it succeeds and "
            f"`{t['artifact_glob']}` exists."
        )
        lines.append("")
    lines.append("BUILD PLAN — ordered tasks, each owns specific files; implement every task:")
    lines.append(json.dumps(plan, indent=2))
    return "\n".join(lines)


async def run(max_turns: int) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, query

    scope = json.loads((DEV / "scope.json").read_text())
    plan = json.loads((DEV / "plan.json").read_text())
    opts = ClaudeAgentOptions(
        allowed_tools=["Write", "Edit", "Read", "Bash"],
        permission_mode="bypassPermissions",
        cwd="/out",
        max_turns=max_turns,
        setting_sources=[],
    )
    prompt = build_prompt(scope, plan)
    repair_file = DEV / "repair.txt"
    if repair_file.exists():
        prompt = REPAIR_PREFIX.format(diagnostics=repair_file.read_text()[:4000]) + prompt
    result_usage = None  # the terminal ResultMessage carries CUMULATIVE usage for the run
    cost = None
    messages = 0
    err = None
    try:
        async for msg in query(prompt=prompt, options=opts):
            messages += 1
            if getattr(msg, "total_cost_usd", None) is not None:
                # ResultMessage: cumulative cost + usage (the per-turn AssistantMessage
                # usages are NOT summed — the ResultMessage already totals them).
                cost = msg.total_cost_usd
                result_usage = getattr(msg, "usage", None)
    except Exception:
        err = traceback.format_exc()[-1500:]

    tin, tout = input_output_tokens(result_usage)
    DEV.mkdir(parents=True, exist_ok=True)
    (DEV / "result.json").write_text(json.dumps({
        "ok_stream": err is None,
        "messages": messages,
        "tokens_in": tin,
        "tokens_out": tout,
        "cost_usd": cost,
        "usage": result_usage,  # raw cumulative breakdown (cache create/read) for transparency
        "error": err,
    }))


if __name__ == "__main__":
    import anyio

    ap = argparse.ArgumentParser()
    ap.add_argument("--max-turns", type=int, default=40)
    args = ap.parse_args()
    anyio.run(run, args.max_turns)
