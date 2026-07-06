"""generate_structured must feed a ValidationError back to the model and retry — a complex
SystemDesign trips cross-field invariants (producer not in `provides`) an appreciable fraction
of the time, and a one-shot emit turned that into design_failed on a live run (2026-07-06)."""

import pytest
from pydantic import BaseModel, model_validator

from devagent.llm import generate_structured


class _Pair(BaseModel):
    a: int
    b: int

    @model_validator(mode="after")
    def _b_greater(self):
        if self.b <= self.a:
            raise ValueError("b must be greater than a")
        return self


class _FlakyLLM:
    """First emit violates the model invariant; subsequent emits are valid."""
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.prompts = []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"])
        block = type("B", (), {"type": "tool_use", "name": "emit",
                               "input": self._payloads.pop(0)})
        usage = type("U", (), {"input_tokens": 5, "output_tokens": 7})
        return type("R", (), {"content": [block], "usage": usage})


def test_validation_error_is_fed_back_and_retried():
    llm = _FlakyLLM([{"a": 2, "b": 1}, {"a": 1, "b": 2}])
    obj, usage = generate_structured("make a pair", _Pair, client=llm)
    assert (obj.a, obj.b) == (1, 2)
    assert len(llm.prompts) == 2
    retry_msgs = llm.prompts[1]
    # the retry conversation carries the bad emit AND the validation error text
    assert any("b must be greater than a" in str(m) for m in retry_msgs)
    assert usage["tokens_in"] == 10 and usage["tokens_out"] == 14   # summed across attempts


def test_persistent_validation_failure_raises_after_retries():
    llm = _FlakyLLM([{"a": 2, "b": 1}] * 3)
    with pytest.raises(Exception, match="b must be greater than a"):
        generate_structured("make a pair", _Pair, client=llm)
    assert len(llm.prompts) == 3          # initial + 2 retries, then give up
