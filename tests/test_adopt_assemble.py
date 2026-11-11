"""Assemble — composing a SystemDesign from a survey and map fragments.

The properties that matter are honesty and wiring. Adopt produces an artifact other tooling will
consume, so: it must never quietly omit a service, and it must never present an unmapped service
as if the code had been read.
"""

import pytest

from devagent.adopt import Fragment, GateFailure, ServiceRoot, Survey, compose


# ---------------------------------------------------------------- the two new gates

def test_gate_refuses_a_design_that_omits_a_surveyed_service(tmp_path):
    """Otherwise "the design validates" is achievable by dropping whatever was hard."""
    survey = Survey(repo="/x", services=[
        ServiceRoot(name="api", path="api", kind="maven-module", deployable=True),
        ServiceRoot(name="web", path="web", kind="node-app", deployable=True)])
    original = compose(survey, [])
    assert len(original.services) == 2

    survey.services.append(ServiceRoot(name="ghost", path="g", kind="node-app", deployable=True))
    from devagent.adopt.assemble import gate_every_service_present
    with pytest.raises(GateFailure, match="omits surveyed service"):
        gate_every_service_present(original, survey)


def test_something_consumed_but_not_owned_is_recorded_as_external(tmp_path):
    """A system that calls an identity provider or another team's API does not contain it in
    its repos. Refusing those would make adoption impossible for any integrated system, so they
    are findings, not failures."""
    survey = Survey(repo="/x", services=[
        ServiceRoot(name="web", path="web", kind="node-app", deployable=True)])
    frag = Fragment(service="web", prd_slice="A frontend that calls an identity provider." * 2,
                    consumes=("identity-oauth2",))
    externals = []
    design = compose(survey, [frag], externals=externals)
    assert externals == [{"contract": "identity-oauth2", "consumed_by": ["web"]}]
    web = design.services[0]
    assert web.consumes == [], "an unowned contract is not intra-system wiring"


def test_gate_still_refuses_a_contract_whose_declared_producer_is_absent():
    """The half that IS a wiring hole: a contract naming a producer not in the design."""
    from devagent.adopt.assemble import _gate_wiring
    from devagent.schema import Contract, ServiceNode

    node = ServiceNode(id="web", name="web", kind="frontend", stack="static-web",
                       prd_slice="x" * 30)
    contract = Contract(id="api", kind="openapi", producer="missing-service")
    with pytest.raises(GateFailure, match="no producing service"):
        _gate_wiring([node], [contract])


@pytest.mark.parametrize("raw,expected", [
    ("user-api:GET /api/user/get", "user-api"),
    ("user-api:POST /openapi/user/login", "user-api"),
    ("identity-oauth2", "identity-oauth2"),
    ("server-api ", "server-api"),
    ("api openapi", "api"),
])
def test_a_consumed_reference_is_normalised_to_a_contract_id(raw, expected):
    """A mapping session is over-specific and may report many endpoint-level ids that are all one
    contract. Unnormalised, they read as many external dependencies when the service consumes a
    single internal API."""

    from devagent.adopt.assemble import contract_id

    assert contract_id(raw) == expected


def test_endpoint_level_consumes_collapse_to_internal_wiring(tmp_path):
    survey = Survey(repo="/x", services=[
        ServiceRoot(name="api", path="a", kind="maven-module", deployable=True),
        ServiceRoot(name="mcp", path="m", kind="node-app", deployable=True)])
    frags = [
        Fragment(service="api", prd_slice="Serves the admin API surface." * 2, kind="backend",
                 provides=({"id": "api", "kind": "openapi", "spec": {}},)),
        Fragment(service="mcp", prd_slice="Tool server calling the admin API." * 2,
                 kind="backend",
                 consumes=("api:GET /api/user/get", "api:POST /api/user/list")),
    ]
    externals = []
    design = compose(survey, frags, externals=externals)
    mcp = next(s for s in design.services if s.name == "mcp")
    assert mcp.consumes == ["api"], "endpoint-level entries are one contract"
    assert mcp.depends_on == ["api"]
    assert externals == [], "an internal contract must not be reported as external"


@pytest.mark.parametrize("java_version,recipe", [("9", "maven-spring-boot-jdk9"),
                                                 (None, "maven-spring-boot")])
def test_an_unmatched_maven_module_needs_the_recipe_for_its_java_version(java_version, recipe):
    from devagent.adopt.assemble import needed_recipe

    svc = ServiceRoot(name="api", path="api", kind="maven-module", deployable=True,
                      evidence={"java_version": java_version})
    assert needed_recipe(svc) == recipe
