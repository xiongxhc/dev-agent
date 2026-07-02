from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.contract_utils import contracts_for_node, openapi_to_checks


def _design():
    return SystemDesign(
        title="t",
        services=[
            ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                        prd_slice="x", provides=["api.openapi"]),
            ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                        prd_slice="y", depends_on=["api"], consumes=["api.openapi"]),
        ],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}, "post": {}}}})],
    )


def test_contracts_for_node_returns_consumed():
    d = _design()
    web = d.services[1]
    got = contracts_for_node(web, d)
    assert [c.id for c in got] == ["api.openapi"]


def test_contracts_for_node_empty_when_none_consumed():
    d = _design()
    api = d.services[0]
    assert contracts_for_node(api, d) == []


def test_openapi_to_checks_maps_get_and_nonget():
    c = Contract(id="c", kind="openapi", producer="api",
                 spec={"paths": {"/api/todos": {"get": {}, "post": {}}}})
    checks = openapi_to_checks(c)
    assert {"kind": "route_status", "route": "/api/todos", "expected_status": 200} in checks
    assert {"kind": "api_json", "route": "/api/todos", "method": "POST"} in checks


def test_openapi_to_checks_ignores_non_openapi():
    c = Contract(id="c", kind="db_schema", producer="api", spec={"tables": {}})
    assert openapi_to_checks(c) == []
