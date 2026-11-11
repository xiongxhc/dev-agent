"""In-container mapper: read one service read-only and print a fragment as JSON.

Mirrors sdk_runner.py, with two deliberate differences. The tool set is READING ONLY — no
Write, no Edit, no Bash — so the session cannot modify or exfiltrate beyond what it is shown;
and the mount is `:ro`, so even a tool that tried would fail. Output is a single JSON object on
stdout, which the host validates with pydantic; nothing here is trusted.

claude_agent_sdk is imported lazily inside run(), so the prompt builder imports on a host that
lacks the SDK.
"""

import argparse
import asyncio
import json
import sys

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


async def run(service: str, retry_context: str | None) -> int:
    from claude_agent_sdk import ClaudeAgentOptions, query

    opts = ClaudeAgentOptions(
        # Reading tools only: a model with tools must not be able to change the code it describes.
        allowed_tools=["Read", "Grep", "Glob"],
        permission_mode="bypassPermissions",
        cwd="/src",
        max_turns=30,
        setting_sources=[],
    )
    text = []
    async for msg in query(prompt=build_prompt(service, retry_context), options=opts):
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
        return asyncio.run(run(args.service, args.retry_context))
    except Exception as e:  # noqa: BLE001 — a failed session is a verdict, not a crash
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
