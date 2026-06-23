"""Structured-output helper over the Anthropic Messages API.

The brain phases (intake/spec/plan) are SHARED across both A/B executor arms and only
emit validated artifacts — they don't run code — so they use the Messages API directly
with forced tool-use to return a pydantic-validated object. The Agent SDK lives in the
build Executor (the self-built arm), not here.

Validation happens at the tool-call boundary: the model is forced to call `emit` with a
schema-shaped payload, and we `model_validate` it — a malformed/partial emit raises.
"""

import os
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# Sonnet by default: capable enough for spec/plan, far cheaper than Opus for a daemon.
DEFAULT_MODEL = os.getenv("DEVAGENT_LLM_MODEL", "claude-sonnet-4-6")


def generate_structured(
    prompt: str,
    schema: Type[T],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int = 4000,
    client: anthropic.Anthropic | None = None,
) -> tuple[T, dict]:
    """Return (validated schema instance, usage dict). `client` is injectable for tests."""
    client = client or anthropic.Anthropic()
    tool = {
        "name": "emit",
        "description": f"Emit a valid {schema.__name__}.",
        "input_schema": schema.model_json_schema(),
    }
    resp = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        system=system or "You produce only the requested structured output.",
        tools=[tool],
        tool_choice={"type": "tool", "name": "emit"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit":
            obj = schema.model_validate(block.input)
            usage = {
                "tokens_in": resp.usage.input_tokens,
                "tokens_out": resp.usage.output_tokens,
            }
            return obj, usage
    raise RuntimeError("model did not emit structured output")
