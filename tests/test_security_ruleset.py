from devagent.security.findings import Finding
from devagent.security.ruleset import gates, partition, GATING_KINDS


def _f(kind, route="/x"):
    return Finding(kind=kind, service="api", route=route, method="POST", severity="high",
                   confidence="high", evidence="e", remediation="r")


def test_gating_kinds_fail_by_default():
    for kind in GATING_KINDS:
        assert gates(_f(kind), open_pairs=set()) is True


def test_advisory_kinds_never_gate():
    for kind in ("weak_registration", "verb_tampering"):
        assert gates(_f(kind), open_pairs=set()) is False


def test_escape_hatch_suppresses_missing_authz_on_declared_route():
    f = _f("missing_authz", route="/auth/register")
    assert gates(f, open_pairs={("/auth/register", "missing_authz")}) is False


def test_escape_hatch_does_not_suppress_mass_assignment():
    # "anyone may sign up" != "a signup may set its own role" — mass-assignment still gates.
    f = _f("mass_assignment", route="/auth/register")
    assert gates(f, open_pairs={("/auth/register", "missing_authz"),
                                ("/auth/register", "mass_assignment")}) is True


def test_partition_splits_gating_from_advisory():
    findings = [_f("mass_assignment"), _f("verb_tampering"), _f("idor")]
    gating, advisory = partition(findings, open_pairs=set())
    assert {g.kind for g in gating} == {"mass_assignment", "idor"}
    assert {a.kind for a in advisory} == {"verb_tampering"}
