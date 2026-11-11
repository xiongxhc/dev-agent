"""Egress allowlist — the deny-by-default host matching (pure; no Docker/network)."""

from types import SimpleNamespace

import pytest

from devagent import egress
from devagent.egress import allowlist, docker_flags, hosts_for_stacks
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


def test_hosts_for_stacks_collects_recipe_hosts_once():
    hosts = hosts_for_stacks(["maven-spring-boot", "node-vite-yarn", "maven-spring-boot", None, "no-such-recipe"])
    assert hosts == ["repo.maven.apache.org", "repo1.maven.org", "registry.yarnpkg.com", ".yarnpkg.com"]


def test_allowlist_adds_extras_to_the_defaults(monkeypatch):
    monkeypatch.delenv("DEVAGENT_EGRESS_ALLOW", raising=False)
    assert allowlist(["nexus.example"]) == ",".join([*DEFAULT_ALLOW, "nexus.example"])
    assert allowlist(["api.anthropic.com"]) == ",".join(DEFAULT_ALLOW)        # no duplicate


def test_allowlist_override_replaces_defaults_but_keeps_extras(monkeypatch):
    monkeypatch.setenv("DEVAGENT_EGRESS_ALLOW", "a.example, b.example")
    assert allowlist(["c.example"]) == "a.example,b.example,c.example"


# --- ensure(): Docker faked by recording every argv --------------------------------------

@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))


def _fake_docker(monkeypatch, *, running="false", label="", others=""):
    """Replace egress._run; return the list of recorded argvs."""
    calls = []

    def fake(args):
        calls.append(list(args))
        out, rc = "", 0
        if args[:3] == ["docker", "network", "inspect"] and "-f" in args:
            out = others
        elif args[:3] == ["docker", "network", "inspect"]:
            rc = 0
        elif args[:2] == ["docker", "inspect"] and "{{.State.Running}}" in args:
            out = running
        elif args[:2] == ["docker", "inspect"] and any("devagent.allow" in a for a in args):
            out = label
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(egress, "_run", fake)
    monkeypatch.delenv("DEVAGENT_EGRESS_ALLOW", raising=False)
    return calls


def _verbs(calls):
    return [c[1] for c in calls]


def test_ensure_starts_the_proxy_with_its_allowlist_as_env_and_label(monkeypatch):
    calls = _fake_docker(monkeypatch, running="false")
    allow = allowlist(["repo1.maven.org"])
    network, url = egress.ensure(extra_hosts=["repo1.maven.org"])
    runs = [c for c in calls if c[:2] == ["docker", "run"]]
    assert len(runs) == 1
    argv = runs[0]
    assert argv[argv.index("-e") + 1] == f"DEVAGENT_EGRESS_ALLOW={allow}"
    assert ["--label", f"devagent.allow={allow}"] == argv[argv.index("--label"):argv.index("--label") + 2]
    assert (network, url) == (egress.NETWORK, f"http://{egress.PROXY}:{egress.PORT}")


def test_ensure_reuses_a_proxy_with_the_same_allowlist(monkeypatch):
    allow = allowlist(["repo1.maven.org"])
    calls = _fake_docker(monkeypatch, running="true", label=allow)
    egress.ensure(extra_hosts=["repo1.maven.org"])
    assert not [c for c in calls if c[:2] == ["docker", "run"]]
    assert not [c for c in calls if c[:2] == ["docker", "rm"]]


def test_ensure_adopts_a_proxy_whose_allowlist_is_a_superset(monkeypatch):
    superset = allowlist(["repo1.maven.org", "registry.yarnpkg.com"])
    calls = _fake_docker(monkeypatch, running="true", label=superset, others="some-build ")
    egress.ensure(extra_hosts=["repo1.maven.org"])
    assert not [c for c in calls if c[:2] == ["docker", "run"]]
    assert not [c for c in calls if c[:2] == ["docker", "rm"]]


def test_ensure_refuses_to_replace_a_proxy_other_containers_use(monkeypatch):
    calls = _fake_docker(monkeypatch, running="true", label="other.example",
                         others=f"{egress.PROXY} some-build ")
    with pytest.raises(RuntimeError, match="different allowlist"):
        egress.ensure(extra_hosts=["repo1.maven.org"])
    assert not [c for c in calls if c[:2] == ["docker", "run"]]
    assert not [c for c in calls if c[:2] == ["docker", "rm"]]


def test_ensure_replaces_an_idle_proxy_with_a_different_allowlist(monkeypatch):
    calls = _fake_docker(monkeypatch, running="true", label="other.example", others=f"{egress.PROXY} ")
    egress.ensure(extra_hosts=["repo1.maven.org"])
    rm = [i for i, c in enumerate(calls) if c == ["docker", "rm", "-f", egress.PROXY]]
    run = [i for i, c in enumerate(calls) if c[:2] == ["docker", "run"]]
    assert len(rm) == 1 and len(run) == 1 and rm[0] < run[0]


def test_ensure_runs_its_docker_calls_while_holding_the_lock(monkeypatch):
    from contextlib import contextmanager

    events = []

    @contextmanager
    def fake_exclusive(root, name, **kw):
        events.append(("enter", name))
        yield
        events.append(("exit", name))

    calls = _fake_docker(monkeypatch, running="false")
    real = egress._run
    monkeypatch.setattr(egress, "_run", lambda a: (events.append(("docker", a[1])), real(a))[1])
    monkeypatch.setattr(egress.hostlock, "exclusive", fake_exclusive)
    egress.ensure()
    assert calls and events[0] == ("enter", "egress") and events[-1] == ("exit", "egress")
    assert all(e[0] == "docker" for e in events[1:-1]) and len(events) == len(calls) + 2


def test_an_unusable_lock_does_not_stop_ensure(monkeypatch):
    calls = _fake_docker(monkeypatch, running="false")
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", "/dev/null/runs")      # lock dir cannot be created
    assert egress.ensure()[0] == egress.NETWORK
    assert [c for c in calls if c[:2] == ["docker", "run"]]


def test_a_lock_timeout_stops_ensure_without_touching_docker(monkeypatch):
    calls = _fake_docker(monkeypatch, running="false")

    class TimesOut:
        def __enter__(self):
            raise TimeoutError("waited 180s for the 'egress' lock")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(egress.hostlock, "exclusive", lambda *a, **kw: TimesOut())
    with pytest.raises(TimeoutError):
        egress.ensure()
    assert calls == []
