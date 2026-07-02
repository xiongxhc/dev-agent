import pytest
from pydantic import ValidationError
from devagent.schema import Contract, ServiceNode, SystemDesign


def _api_web_design():
    """A valid 2-service design: web (consumer) depends on api (producer of an openapi contract)."""
    return SystemDesign(
        title="Todo system",
        services=[
            ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                        prd_slice="A JSON API for todos.", provides=["api.openapi"]),
            ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                        prd_slice="A UI listing todos from the API.",
                        depends_on=["api"], consumes=["api.openapi"]),
        ],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}}}})],
    )


def test_valid_design_constructs_and_freezes_contract_version():
    d = _api_web_design()
    assert [s.id for s in d.services] == ["api", "web"]
    assert d.contracts[0].version == 1  # frozen in M14


def test_requires_at_least_one_service():
    with pytest.raises(ValidationError):
        SystemDesign(title="t", services=[])


def test_rejects_duplicate_service_ids():
    with pytest.raises(ValidationError):
        SystemDesign(title="t", services=[
            ServiceNode(id="a", name="a", kind="backend", stack="node-express", prd_slice="x"),
            ServiceNode(id="a", name="a2", kind="backend", stack="node-express", prd_slice="y"),
        ])


def test_rejects_contract_with_unknown_producer():
    with pytest.raises(ValidationError):
        SystemDesign(title="t",
            services=[ServiceNode(id="api", name="api", kind="backend",
                                  stack="node-express", prd_slice="x", provides=["c"])],
            contracts=[Contract(id="c", kind="openapi", producer="ghost")])


def test_rejects_provides_of_unknown_contract():
    with pytest.raises(ValidationError):
        SystemDesign(title="t",
            services=[ServiceNode(id="api", name="api", kind="backend",
                                  stack="node-express", prd_slice="x", provides=["nope"])])


def test_rejects_producer_that_does_not_list_contract_in_provides():
    # contract says producer=api, but api does not list it in provides -> inconsistent
    with pytest.raises(ValidationError):
        SystemDesign(title="t",
            services=[ServiceNode(id="api", name="api", kind="backend",
                                  stack="node-express", prd_slice="x")],
            contracts=[Contract(id="c", kind="openapi", producer="api")])


def test_rejects_depends_on_unknown_service():
    with pytest.raises(ValidationError):
        SystemDesign(title="t",
            services=[ServiceNode(id="web", name="web", kind="frontend",
                                  stack="node-vite-react", prd_slice="x", depends_on=["ghost"])])
