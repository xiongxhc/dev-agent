"""Runs INSIDE the M2 sandbox container. Drives the Claude Agent SDK to build the app
per the frozen Spec+Plan (written by the host to /out/.devagent/), writing the app to
/out. Reports usage + any error to /out/.devagent/result.json.

setting_sources=[] is mandatory (proven in de-risk) so the in-container SDK never tries
to load host ~/.claude settings/hooks. Network egress (api.anthropic.com + npm) is
controlled by the host's `docker run` flags, not here."""

import argparse
import json
import traceback
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, query

OUT = Path("/out")
DEV = OUT / ".devagent"

PROMPT = """You are building a {stack} web app. Write ALL files under the current working \
directory (/out). Never write outside /out.

SPEC (JSON):
{spec}

BUILD PLAN — ordered tasks, each owns specific files; implement every task:
{plan}

Instructions:
- Scaffold a Vite + React + Tailwind project: package.json (PIN exact dependency versions \
— no ^ or ~), vite.config, tailwind.config, postcss.config, index.html, and src/.
- Implement every page in the spec and satisfy every acceptance check (each route must \
render; each listed CSS selector must exist in the DOM).
- Then run `pnpm install` and `pnpm build`. Fix any errors and re-run until the build \
succeeds and a dist/ directory is produced.
- Keep it minimal and idiomatic. Stop once `pnpm build` passes.
"""


async def run(max_turns: int) -> None:
    spec = json.loads((DEV / "spec.json").read_text())
    plan = json.loads((DEV / "plan.json").read_text())
    opts = ClaudeAgentOptions(
        allowed_tools=["Write", "Edit", "Read", "Bash"],
        permission_mode="bypassPermissions",
        cwd="/out",
        max_turns=max_turns,
        setting_sources=[],
    )
    prompt = PROMPT.format(
        stack=spec.get("stack", "vite-react-tailwind"),
        spec=json.dumps(spec, indent=2),
        plan=json.dumps(plan, indent=2),
    )
    tin = tout = 0
    cost = None
    messages = 0
    err = None
    try:
        async for msg in query(prompt=prompt, options=opts):
            messages += 1
            usage = getattr(msg, "usage", None)
            if isinstance(usage, dict):
                tin = usage.get("input_tokens") or tin
                tout = usage.get("output_tokens") or tout
            c = getattr(msg, "total_cost_usd", None)
            if c is not None:
                cost = c
    except Exception:
        err = traceback.format_exc()[-1500:]

    DEV.mkdir(parents=True, exist_ok=True)
    (DEV / "result.json").write_text(json.dumps({
        "ok_stream": err is None,
        "messages": messages,
        "tokens_in": tin,
        "tokens_out": tout,
        "cost_usd": cost,
        "error": err,
    }))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-turns", type=int, default=40)
    args = ap.parse_args()
    anyio.run(run, args.max_turns)
