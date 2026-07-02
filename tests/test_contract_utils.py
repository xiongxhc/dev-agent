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


def test_openapi_to_checks_get_only_non_templated():
    c = Contract(id="c", kind="openapi", producer="api",
                 spec={"paths": {"/api/todos": {"get": {}, "post": {}},
                                 "/api/todos/{id}": {"get": {}},
                                 "/api/health": {"get": {}, "parameters": []}}})
    checks = openapi_to_checks(c)
    routes = {(ch["route"], ch["kind"]) for ch in checks}
    assert ("/api/todos", "route_status") in routes         # GET kept
    assert ("/api/health", "route_status") in routes        # GET kept; `parameters` key ignored
    assert all(ch["kind"] == "route_status" for ch in checks)      # no non-GET, no api_json
    assert not any(ch["route"] == "/api/todos/{id}" for ch in checks)   # templated skipped
    assert sum(1 for ch in checks if ch["route"] == "/api/todos") == 1  # POST produced no check


def test_openapi_to_checks_guards_non_dict_path_item():
    c = Contract(id="c", kind="openapi", producer="api",
                 spec={"paths": {"/x": "not-a-dict", "/y": {"get": {}}}})
    assert [ch["route"] for ch in openapi_to_checks(c)] == ["/y"]   # non-dict skipped, no crash


def test_openapi_to_checks_ignores_non_openapi():
    c = Contract(id="c", kind="db_schema", producer="api", spec={"tables": {}})
    assert openapi_to_checks(c) == []
