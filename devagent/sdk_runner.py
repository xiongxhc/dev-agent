"""Runs INSIDE the M2 sandbox container. Drives the Claude Agent SDK to build the app
per the frozen Scope+Plan (written by the host to /out/.devagent/), writing the app to
/out. Reports usage + any error to /out/.devagent/result.json.

setting_sources=[] is mandatory (proven in de-risk) so the in-container SDK never tries
to load host ~/.claude settings/hooks. Network egress (api.anthropic.com + npm) is
controlled by the host's `docker run` flags, not here."""

import argparse
import json
import os
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


def _auth_contract_line(auth: dict) -> str:
    """The EXACT login flow the verifier executes for one AuthFlow; the app's endpoints must
    honor it. Covers both credential styles (bearer token vs session cookie)."""
    reg_body = auth.get("register_body") or auth.get("login_body")
    reg = (f"POSTs {json.dumps(reg_body)} to {auth['register_route']} to create the user, then "
           if auth.get("register_route") else "")
    if auth.get("mode") == "cookie":
        creds = ("On a valid login it sets a session cookie (Set-Cookie); protected routes accept "
                 "that cookie back on the request.")
    else:
        creds = (f"reads the token at response path '{auth.get('token_json_path')}'. Protected routes "
                 f"accept `{auth.get('header', 'Authorization')}: {auth.get('scheme', 'Bearer')} <token>`.")
    return (
        f"AUTH CONTRACT — the verifier authenticates with this EXACT flow: {reg}"
        f"POSTs {json.dumps(auth.get('login_body'))} to {auth['login_route']}. {creds} "
        "Accept EXACTLY these fields — do NOT require any others (e.g. no email if none is sent)."
    )


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
        if t.get("acceptance_checks"):
            lines.append("This target is VERIFIED by these machine checks — build to satisfy them EXACTLY "
                         "(same routes, methods, and JSON shapes):")
            lines.append(json.dumps(t["acceptance_checks"], indent=2))
        if t.get("_consumed_contracts"):
            lines.append(
                "CONSUMED CONTRACTS (read-only — this service calls other services; build against "
                "these interfaces EXACTLY, do not redefine them):")
            lines.append(json.dumps(t["_consumed_contracts"], indent=2))
        if t.get("auth"):
            lines.append(_auth_contract_line(t["auth"]))
        actors = t.get("actors") or []
        if actors:
            lines.append(
                "AUTHZ MATRIX — these named actors each authenticate with their own flow; honor "
                "their roles so protected routes return the right status per actor (e.g. an admin "
                "gets 200 on /admin, a member gets 403). Each actor's login contract:"
            )
            for a in actors:
                role = f" (role: {a['role']})" if a.get("role") else ""
                lines.append(f"  - actor '{a.get('name')}'{role}: " + _auth_contract_line(a))
        if (t.get("detail") or {}).get("idp"):
            d = t["detail"]
            lines.append(
                "FEDERATED AUTH — this target authenticates against an external identity provider "
                f"(a seeded mock IdP container, target '{d['idp']}'). Read its issuer/base URL from "
                f"process.env['{d.get('idp_env', 'OIDC_ISSUER')}'] and validate tokens against it. Do "
                "NOT hardcode a provider URL and do NOT require real interactive consent — verify runs "
                "sealed against the seeded mock, never a real IdP."
            )
        lines.append("")
    lines.append("BUILD PLAN — ordered tasks, each owns specific files; implement every task:")
    lines.append(json.dumps(plan, indent=2))
    return "\n".join(lines)


async def run(max_turns: int, target: str | None = None) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, query

    # Parallel build (M12): a single build-target's scope+plan slice live under
    # .devagent/build/<target>/, and the result is written there. Without --target the
    # whole-project scope at .devagent/ is built (the original sequential path).
    src = (DEV / "build" / target) if target else DEV
    scope = json.loads((src / "scope.json").read_text())
    plan = json.loads((src / "plan.json").read_text())
    opts = ClaudeAgentOptions(
        allowed_tools=["Write", "Edit", "Read", "Bash"],
        permission_mode="bypassPermissions",
        cwd="/out",
        max_turns=max_turns,
        setting_sources=[],
        # build model is set per-run for the model-quality A/B; unset => the SDK default.
        **({"model": os.environ["DEVAGENT_BUILD_MODEL"]} if os.environ.get("DEVAGENT_BUILD_MODEL") else {}),
    )
    prompt = build_prompt(scope, plan)
    repair_file = DEV / "repair.txt"   # repair diagnostics are project-global (verify output)
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
    src.mkdir(parents=True, exist_ok=True)
    (src / "result.json").write_text(json.dumps({
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
    ap.add_argument("--target", default=None,
                    help="build only this target (M12 parallel build); default = whole project")
    args = ap.parse_args()
    anyio.run(run, args.max_turns, args.target)
