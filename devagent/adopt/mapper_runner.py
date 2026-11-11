"""In-container mapper: read one service and print a fragment as JSON.

A model with tools must not be able to change the code it is describing, or read anything but
that code. Besides the host's `:ro` mount of the repo at /src, the session is locked three ways:

1. `disallowed_tools` names every built-in tool the tool references document, except Read, Grep
   and Glob, plus `mcp__*` for every MCP tool. A named tool is removed from the request and a
   call to it is denied in every permission mode. A tool the list does not name is untouched by
   this lock.
2. `permission_mode="dontAsk"` with `allowed_tools=["Read", "Grep", "Glob"]`: the three are
   pre-approved, and a call that would otherwise ask for approval is denied instead. A call that
   needs no approval still runs, so this lock alone does not confine the session.
3. A `PreToolUse` hook on every tool call denies any tool that is not Read, Grep or Glob, and any
   of those three whose target resolves (symlinks followed) outside /src or whose file pattern
   could match outside it. Anything it does not recognise is denied. Its matcher sets
   `on_failure="block"`, so a hook that fails blocks the call; an SDK too old to support that
   option is refused before the session starts.

Output is a single JSON object on stdout, which the host validates; nothing here is trusted.
claude_agent_sdk is imported lazily inside run(), so the options, the guard and the prompt
builder import on a host that lacks the SDK.
"""

import argparse
import asyncio
import json
import os
import re
import sys

ROOT = "/src"
READ_TOOLS = ("Read", "Grep", "Glob")

#: The built-in tools, other than the three reading tools, by the tool name the Claude Code tools
#: reference (code.claude.com/docs/en/tools-reference) and the Agent SDK's tool input types
#: (code.claude.com/docs/en/agent-sdk/typescript#tool-input-types) give it; a deny rule matches
#: the tool name, not the section heading. "mcp__*" denies every MCP tool. A tool missing from
#: this list is still denied by the guard.
DENIED_TOOLS = (
    "Agent", "Artifact", "AskUserQuestion", "Bash", "CronCreate", "CronDelete", "CronList",
    "Edit", "EndConversation", "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree",
    "ListAgents", "ListMcpResourcesTool", "LSP", "Monitor", "NotebookEdit",
    "PowerShell", "Projects", "PushNotification", "ReadMcpResourceDirTool",
    "ReadMcpResourceTool", "RefreshMcpTools", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "SendFeedback", "SendMessage", "SendUserFile", "ShareOnboardingGuide",
    "ShowOnboardingRolePicker", "Skill", "SubagentHandback", "TaskCreate", "TaskGet",
    "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "TodoWrite", "ToolSearch",
    "WaitForMcpServers", "WebFetch", "WebSearch", "Workflow", "Write",
    "mcp__*",
)

#: The first Python Agent SDK release whose hook matchers accept on_failure="block".
SDK_FLOOR = (0, 2, 177)

SYSTEM = ("You are a senior engineer reading an unfamiliar service to describe it accurately. "
          "You never guess: if the code does not show something, you omit it.")

PROMPT = """\
Read the service at /src/{service} (mounted read-only) and describe it.

Return ONE JSON object, nothing else, with these fields:
  summary     A precise description of what this service does, in 3-6 sentences: its
              responsibility, the surfaces it exposes, and the datastores or services it talks
              to. Describe only what the code shows.
  kind        "backend", "frontend" or "datastore".
  provides    The HTTP API it implements, as [{{"id": "<service>-api", "kind": "openapi",
              "spec": {{...minimal OpenAPI: paths and methods you can see...}}}}]. Omit or use
              [] if it exposes no HTTP surface.
  consumes    Contract ids it calls, as strings, if any are evident.
  depends_on  Names of sibling services it depends on, if any are evident.

Read the entry points, controllers/routes and configuration first. Do not speculate about
behaviour you cannot see in the code.{retry}
"""


def build_prompt(service: str, retry_context: str | None) -> str:
    """The session's prompt. `service` comes from repo files and the retry context from a
    validator's message, so both are substituted as values: braces in them stay literal text."""
    retry = ""
    if retry_context:
        retry = ("\n\nYour previous answer failed validation:\n" + retry_context
                 + "\nReturn a corrected JSON object that satisfies every constraint.")
    return PROMPT.format(service=service, retry=retry)


def session_options(root: str = ROOT) -> dict:
    """The session's options as plain values; run() adds the guard hook."""
    return {
        "system_prompt": SYSTEM,
        "allowed_tools": list(READ_TOOLS),
        "disallowed_tools": list(DENIED_TOOLS),
        "permission_mode": "dontAsk",
        "cwd": root,
        "max_turns": 30,
        "setting_sources": [],
    }


def _inside(target: str, root: str) -> bool:
    """Does `target` (relative paths are relative to `root`) resolve, symlinks followed, to
    `root` or below it?"""
    if "\x00" in target or target.startswith("~"):
        return False
    real_root = os.path.realpath(root)
    real = os.path.realpath(target if os.path.isabs(target) else os.path.join(root, target))
    return real == real_root or real.startswith(real_root.rstrip("/") + "/")


#: More brace alternatives than this are not expanded; the pattern is refused instead.
MAX_ALTERNATIVES = 256


def _alternatives(pattern: str):
    """Each pattern a glob's brace alternatives (`{a,b}`, nested too) expand to. A brace with
    no closing match is literal text."""
    for start, ch in enumerate(pattern):
        if ch != "{":
            continue
        depth, last, parts = 0, start + 1, []
        for i in range(start, len(pattern)):
            c = pattern[i]
            if c == "{":
                depth += 1
            elif c == "," and depth == 1:
                parts.append(pattern[last:i])
                last = i + 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    parts.append(pattern[last:i])
                    for part in parts:
                        yield from _alternatives(pattern[:start] + part + pattern[i + 1:])
                    return
    yield pattern


def _climbs(pattern: str) -> bool:
    """A file pattern any of whose brace alternatives starts outside the search directory or
    steps up out of it. A glob matches every alternative, so each is judged on its own."""
    for n, alt in enumerate(_alternatives(pattern)):
        if n >= MAX_ALTERNATIVES or alt.startswith(("/", "~")) or ".." in alt.split("/"):
            return True
    return False


def decide(tool_name: str, tool_input: dict, root: str = ROOT) -> str | None:
    """Why this tool call is denied, or None to let it through. Default deny: anything not
    recognised as a read inside `root` is refused. Input field names are the Agent SDK's
    (Read: file_path; Grep: pattern, path, glob; Glob: pattern, path)."""
    if tool_name not in READ_TOOLS:
        return f"only {', '.join(READ_TOOLS)} may run in a mapping session"
    if not isinstance(tool_input, dict):
        return "the tool input is not an object"
    if tool_name == "Read":
        target = tool_input.get("file_path")
        if not isinstance(target, str) or not target:
            return "Read needs a file_path"
    else:
        if not isinstance(tool_input.get("pattern"), str):
            return f"{tool_name} needs a pattern"
        target = tool_input.get("path")
        if target is None:
            target = root                       # no path searches the working directory
        elif not isinstance(target, str) or not target:
            return f"{tool_name} path must be a non-empty string"
        file_pattern = tool_input["pattern"] if tool_name == "Glob" else tool_input.get("glob")
        if file_pattern is not None and (not isinstance(file_pattern, str)
                                         or _climbs(file_pattern)):
            return f"{tool_name} file patterns must stay inside {root}"
    if not _inside(target, root):
        return f"{tool_name} may only read inside {root}"
    return None


async def guard(input_data, tool_use_id, context):
    """The PreToolUse hook: the documented deny output for a denied call, {} otherwise. An
    error here propagates, and the matcher's on_failure="block" turns it into a block."""
    reason = decide(input_data.get("tool_name"), input_data.get("tool_input"))
    if reason is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def sdk_supports(version: str) -> bool:
    """Is this a final release at or above SDK_FLOOR? Anything unparseable fails closed."""
    if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+)*", version):
        return False
    parts = tuple(int(p) for p in version.split("."))
    return parts + (0,) * (len(SDK_FLOOR) - len(parts)) >= SDK_FLOOR


def _installed_sdk_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("claude-agent-sdk")
    except PackageNotFoundError:
        return None


def _require_sdk() -> None:
    """Refuse to start on an SDK that cannot honour the guard's on_failure="block"."""
    installed = _installed_sdk_version()
    if installed is None or not sdk_supports(installed):
        floor = ".".join(map(str, SDK_FLOOR))
        raise RuntimeError(f"claude-agent-sdk {installed or '(not installed)'} cannot block a "
                           f"tool call when the guard hook fails; {floor} or later is required")


async def run(service: str, retry_context: str | None) -> int:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

    opts = ClaudeAgentOptions(
        **session_options(),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard], on_failure="block")]},
    )
    text = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(build_prompt(service, retry_context))
        async for msg in client.receive_response():
            for block in getattr(msg, "content", None) or []:
                chunk = getattr(block, "text", None)
                if chunk:
                    text.append(chunk)
    blob = "\n".join(text)
    start, end = blob.find("{"), blob.rfind("}")
    if start == -1 or end <= start:
        print(json.dumps({"error": "no JSON object in the session output"}))
        return 1
    print(blob[start:end + 1].replace("\n", " "))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", required=True)
    ap.add_argument("--retry-context", default=None)
    args = ap.parse_args()
    try:
        _require_sdk()
        return asyncio.run(run(args.service, args.retry_context))
    except Exception as e:  # noqa: BLE001 — a failed session is a verdict, not a crash
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
