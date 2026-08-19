"""Egress allowlist — the deny-by-default host matching (pure; no Docker/network)."""

from devagent.egress import docker_flags
from devagent.egress_proxy import DEFAULT_ALLOW, host_allowed


def test_docker_flags_empty_when_disabled():
    assert docker_flags(None, None) == []


def test_docker_flags_set_network_and_proxy():
    flags = docker_flags("devagent-egress", "http://devagent-proxy:3128")
    assert "--network" in flags and "devagent-egress" in flags
    joined = " ".join(flags)
    assert "HTTPS_PROXY=http://devagent-proxy:3128" in joined
    assert "https_proxy=http://devagent-proxy:3128" in joined  # lower-case too


def test_exact_and_subdomain_allow():
    assert host_allowed("api.anthropic.com", DEFAULT_ALLOW)
    assert host_allowed("registry.npmjs.org", DEFAULT_ALLOW)
    assert host_allowed("statsig.anthropic.com", DEFAULT_ALLOW)  # via .anthropic.com
    assert host_allowed("REGISTRY.NPMJS.ORG", DEFAULT_ALLOW)     # case-insensitive
    assert host_allowed("api.anthropic.com.", DEFAULT_ALLOW)     # trailing dot tolerated


def test_denies_everything_else():
    for bad in ["example.com", "evil.com", "anthropic.com.evil.com", "notanthropic.com", "", None]:
        assert not host_allowed(bad, DEFAULT_ALLOW), bad


def test_no_substring_bypass():
    # a host that merely contains an allowed name but isn't a subdomain must be denied
    assert not host_allowed("api.anthropic.com.attacker.net", DEFAULT_ALLOW)
    assert not host_allowed("xregistry.npmjs.org.evil", DEFAULT_ALLOW)


def test_leading_dot_does_not_match_bare_suffix_typo():
    assert host_allowed("a.b.anthropic.com", [".anthropic.com"])
    assert not host_allowed("fakeanthropic.com", [".anthropic.com"])


def test_deepseek_api_allowed_for_the_deepseek_arm():
    assert host_allowed("api.deepseek.com", DEFAULT_ALLOW)
    assert not host_allowed("deepseek.com", DEFAULT_ALLOW)       # only the API host
    assert not host_allowed("api.deepseek.com.evil.com", DEFAULT_ALLOW)
