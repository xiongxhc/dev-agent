"""make_executor — the single validated resolver for DEVAGENT_EXECUTOR. A typo'd arm
name must fail loudly, never silently fall back to the (expensive) sdk arm."""

import pytest

from devagent.executor import make_executor
from devagent.executor_deepseek import DeepSeekExecutor
from devagent.executor_sdk import SdkExecutor
from devagent.managed_executor import ManagedExecutor


def test_unknown_arm_raises():
    with pytest.raises(ValueError, match="unknown executor 'deepsek'"):
        make_executor("deepsek")


def test_sdk_arm_wires_network_proxy_and_model():
    ex = make_executor("sdk", network="devagent-egress",
                       proxy_url="http://devagent-proxy:3128", model="claude-sonnet-5")
    assert isinstance(ex, SdkExecutor) and not isinstance(ex, DeepSeekExecutor)
    assert ex.network == "devagent-egress"
    assert ex.proxy_url == "http://devagent-proxy:3128"
    assert ex.model == "claude-sonnet-5"


def test_managed_arm():
    assert isinstance(make_executor("managed"), ManagedExecutor)


def test_deepseek_arm_ignores_claude_build_model():
    # cfg.build_model carries Claude model names; the deepseek arm's model comes from
    # DEVAGENT_DEEPSEEK_MODEL alone, so a global DEVAGENT_BUILD_MODEL must not leak in.
    ex = make_executor("deepseek", network="devagent-egress",
                       proxy_url="http://devagent-proxy:3128", model="claude-sonnet-5")
    assert isinstance(ex, DeepSeekExecutor)
    assert ex.model == "deepseek-v4-pro"
    assert ex.network == "devagent-egress"
