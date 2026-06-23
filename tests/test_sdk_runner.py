"""Pure token-accounting from the SDK's cumulative usage dict. No SDK/container needed —
the shape below is a REAL claude_agent_sdk ResultMessage.usage captured from a live probe."""

from devagent.sdk_runner import input_output_tokens

# Real ResultMessage.usage from a trivial "say hi" run — note cache_read dwarfs input.
REAL_USAGE = {
    "input_tokens": 2341,
    "cache_creation_input_tokens": 183,
    "cache_read_input_tokens": 15246,
    "output_tokens": 4,
    "service_tier": "standard",
}


def test_input_includes_cache_create_and_read():
    tin, tout = input_output_tokens(REAL_USAGE)
    assert tin == 2341 + 183 + 15246   # the cache tokens the old code dropped
    assert tout == 4


def test_handles_missing_and_none_fields():
    assert input_output_tokens({"input_tokens": 10, "output_tokens": 2}) == (10, 2)
    assert input_output_tokens({"cache_read_input_tokens": None, "input_tokens": 5}) == (5, 0)


def test_none_usage_is_zero():
    assert input_output_tokens(None) == (0, 0)
    assert input_output_tokens("not a dict") == (0, 0)
