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


# ---------- one-flow check derivation (2026-07-03) ----------

def _polls_contract():
    from devagent.schema import Contract
    return Contract(id="openapi_polls", kind="openapi", producer="api", spec={
        "paths": {
            "/polls": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "question": {"type": "string"}}}}}}}}},
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "required": ["question", "options"],
                        "properties": {"question": {"type": "string"},
                                       "options": {"type": "array",
                                                   "items": {"type": "string"}}}}}}},
                    "responses": {"201": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"id": {"type": "integer"},
                                                         "question": {"type": "string"}}}}}}}},
            },
            "/polls/{pollId}/options/{optionId}/vote": {
                "post": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"id": {"type": "integer"},
                                                     "votes": {"type": "integer"}}}}}}}}},
        }})


def test_derive_checks_orders_mutations_before_shape_asserts():
    from devagent.contract_utils import derive_checks
    checks = derive_checks(_polls_contract())
    kinds = [(c["kind"], c.get("method", "GET"), c["route"]) for c in checks]
    assert kinds == [
        ("route_status", "GET", "/polls"),                                # safe on empty store
        ("api_json", "POST", "/polls"),                                   # create first
        ("api_json", "GET", "/polls"),                                    # then assert shape
        ("api_json", "POST", "/polls/1/options/1/vote"),                  # params -> 1
    ]
    post = checks[1]
    assert post["body"] == {"question": "sample question",
                            "options": ["sample options", "sample options"]}
    assert post["json_path"] == "id"                                      # 201 schema first prop
    get = checks[2]
    assert get["json_path"] == "0.id"        # ARRAY root per contract — the polls live-run bug
    vote = checks[3]
    assert vote["json_path"] == "id" and vote["body"] is None


def test_derive_checks_object_root_asserts_object_path():
    from devagent.schema import Contract
    from devagent.contract_utils import derive_checks
    c = Contract(id="x", kind="openapi", producer="api", spec={"paths": {"/session": {
        "get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"user": {"type": "string"}}}}}}}},
        "post": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"user": {"type": "string"}}}}}}}}}}})
    checks = derive_checks(c)
    gets = [k for k in checks if k["kind"] == "api_json" and k["method"] == "GET"]
    assert gets and gets[0]["json_path"] == "user"       # object root -> plain key, no "0."


def test_derive_checks_non_openapi_is_empty():
    from devagent.schema import Contract
    from devagent.contract_utils import derive_checks
    assert derive_checks(Contract(id="d", kind="db_schema", producer="db", spec={})) == []


def test_sample_body_is_validator_friendly():
    from devagent.contract_utils import sample_body
    body = sample_body({"type": "object", "required": ["email", "password"], "properties": {
        "email": {"type": "string"}, "password": {"type": "string"},
        "nickname": {"type": "string"}}})
    assert body == {"email": "sample@example.com", "password": "Sample-Passw0rd-1!"}
    assert sample_body({"type": "array", "items": {"type": "integer"}}) == [1, 1]
    assert sample_body({"enum": ["member", "admin"]}) == "member"


def test_derive_persistence_check_uses_first_post_get_pair():
    from devagent.contract_utils import derive_persistence_check
    p = derive_persistence_check(_polls_contract())
    assert p["kind"] == "persistence_survives_restart"
    assert p["route"] == "/polls" and p["verify_route"] == "/polls"
    assert p["json_path"] == "id" and p["method"] == "POST"


def test_derive_integration_checks_maps_producers_and_probes_frontends():
    from devagent.schema import ServiceNode, SystemDesign
    from devagent.contract_utils import derive_integration_checks
    design = SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="x", provides=["openapi_polls"]),
        ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="y", depends_on=["api"], consumes=["openapi_polls"])],
        contracts=[_polls_contract()])
    checks = derive_integration_checks(design)
    assert [c.service for c in checks] == ["api", "api", "api", "api", "web"]
    assert checks[-1].route == "/" and checks[-1].json_path is None
    assert all(c.service == "api" for c in checks if c.route.startswith("/polls"))
