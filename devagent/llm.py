"""Structured-output helper over the Anthropic Messages API.

The brain phases (intake/spec/plan) are SHARED across both A/B executor arms and only
emit validated artifacts — they don't run code — so they use the Messages API directly
with forced tool-use to return a pydantic-validated object. The Agent SDK lives in the
build Executor (the self-built arm), not here.

Validation happens at the tool-call boundary: the model is forced to call `emit` with a
schema-shaped payload, and we `model_validate` it. A malformed emit does NOT fail the phase
outright — the ValidationError text is fed back and the model retries (cap 2). Complex
schemas with cross-field invariants (SystemDesign's producer/provides wiring) trip one-shot
emits an appreciable fraction of the time; a live run died design_failed on exactly that
(2026-07-06)."""

import json
import os
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Sonnet by default: capable enough for spec/plan, far cheaper than Opus for a daemon.
DEFAULT_MODEL = os.getenv("DEVAGENT_LLM_MODEL", "claude-sonnet-4-6")

_MAX_VALIDATION_RETRIES = 2


def generate_structured(
    prompt: str,
    schema: Type[T],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int = 4000,
    client: anthropic.Anthropic | None = None,
) -> tuple[T, dict]:
    """Return (validated schema instance, usage dict — token counts summed across retries).
    `client` is injectable for tests."""
    client = client or anthropic.Anthropic()
    tool = {
        "name": "emit",
        "description": f"Emit a valid {schema.__name__}.",
        "input_schema": schema.model_json_schema(),
    }
    messages = [{"role": "user", "content": prompt}]
    tokens_in = tokens_out = 0
    for attempt in range(1 + _MAX_VALIDATION_RETRIES):
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            system=system or "You produce only the requested structured output.",
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit"},
            messages=messages,
        )
        tokens_in += resp.usage.input_tokens
        tokens_out += resp.usage.output_tokens
        block = next((b for b in resp.content
                      if getattr(b, "type", None) == "tool_use" and b.name == "emit"), None)
        if block is None:
            raise RuntimeError("model did not emit structured output")
        try:
            obj = schema.model_validate(block.input)
        except ValidationError as e:
            if attempt == _MAX_VALIDATION_RETRIES:
                raise
            # Feed the bad emit + the validator's message back; the model corrects in place.
            messages = messages + [
                {"role": "assistant",
                 "content": f"(previous emit, rejected)\n{json.dumps(block.input)[:4000]}"},
                {"role": "user",
                 "content": f"That {schema.__name__} failed validation:\n{e}\n"
                            "Emit a corrected version that satisfies every constraint."},
            ]
            continue
        return obj, {"tokens_in": tokens_in, "tokens_out": tokens_out}
    raise RuntimeError("unreachable")  # loop always returns or raises
