from devagent.security.findings import Finding
from devagent.security.ruleset import gates, partition, intentional_open_pairs, GATING_KINDS
from devagent.schema import Contract, ServiceNode, SystemDesign


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


def test_triage_sourced_finding_never_gates():
    # Live regression (2026-07-10): a flawless api was failed to integration_failed because the
    # triage LLM emitted high-confidence gating-kind findings by READING the frozen contract.
    # Only deterministic probes (source="probe") may gate; triage speculation must not, or the
    # M23 repair loop re-fires the same finding on every re-verify and can never converge.
    for kind in GATING_KINDS:
        f = Finding(kind=kind, service="api", route="/x", method="POST", severity="critical",
                    confidence="high", evidence="the spec likely accepts extra fields",
                    remediation="r", source="triage")
        assert gates(f, open_pairs=set()) is False


def test_partition_routes_triage_gating_kinds_to_advisory():
    det = _f("mass_assignment")                                   # deterministic -> gates
    spec = Finding(kind="idor", service="api", route="/y", method="GET", severity="high",
                   confidence="high", evidence="ownership must be verified", remediation="r",
                   source="triage")                              # triage -> advisory only
    gating, advisory = partition([det, spec], open_pairs=set())
    assert [g.kind for g in gating] == ["mass_assignment"]
    assert [a.kind for a in advisory] == ["idor"]


# Tests for intentional_open_pairs function

def test_intentional_open_pairs_extracts_escape_hatchable_markers():
    # A design with x-intentionally-open: ["missing_authz"] should return the (route, kind) pair.
    design = SystemDesign(
        title="test",
        services=[ServiceNode(
            id="api", name="api", kind="backend", stack="node-express",
            prd_slice="api spec", provides=["contract1"])],
        contracts=[Contract(
            id="contract1", kind="openapi", producer="api",
            spec={"paths": {
                "/auth/register": {
                    "post": {"x-intentionally-open": ["missing_authz"]}
                }
            }}
        )]
    )
    pairs = intentional_open_pairs(design)
    assert pairs == {("/auth/register", "missing_authz")}


def test_intentional_open_pairs_rejects_non_escape_hatchable_kinds():
    # mass_assignment, idor, verb_tampering are NOT escape-hatchable; should not appear in result.
    design = SystemDesign(
        title="test",
        services=[ServiceNode(
            id="api", name="api", kind="backend", stack="node-express",
            prd_slice="api spec", provides=["contract1"])],
        contracts=[Contract(
            id="contract1", kind="openapi", producer="api",
            spec={"paths": {
                "/auth/register": {
                    "post": {"x-intentionally-open": ["mass_assignment", "idor", "verb_tampering"]}
                }
            }}
        )]
    )
    pairs = intentional_open_pairs(design)
    assert pairs == set()  # None of these kinds are escape-hatchable


def test_intentional_open_pairs_empty_without_markers_or_contracts():
    # No x-intentionally-open markers should return empty set.
    design_no_markers = SystemDesign(
        title="test",
        services=[ServiceNode(
            id="api", name="api", kind="backend", stack="node-express",
            prd_slice="api spec", provides=["contract1"])],
        contracts=[Contract(
            id="contract1", kind="openapi", producer="api",
            spec={"paths": {"/todos": {"get": {}}}}
        )]
    )
    assert intentional_open_pairs(design_no_markers) == set()

    # No contracts at all should return empty set.
    design_no_contracts = SystemDesign(
        title="test",
        services=[ServiceNode(
            id="api", name="api", kind="backend", stack="node-express",
            prd_slice="api spec")]
    )
    assert intentional_open_pairs(design_no_contracts) == set()


def test_intentional_open_pairs_ignores_non_openapi_contracts():
    # A marker on a db_schema contract should be ignored (only openapi contracts are read).
    design = SystemDesign(
        title="test",
        services=[ServiceNode(
            id="api", name="api", kind="backend", stack="node-express",
            prd_slice="api spec", provides=["contract1"])],
        contracts=[Contract(
            id="contract1", kind="db_schema", producer="api",
            spec={"paths": {
                "/some/route": {
                    "post": {"x-intentionally-open": ["missing_authz"]}
                }
            }}
        )]
    )
    pairs = intentional_open_pairs(design)
    assert pairs == set()  # db_schema contracts are not openapi, so marker is ignored
