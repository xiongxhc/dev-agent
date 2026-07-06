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
    assert post["body"] == {"question": "sample_question",
                            "options": ["sample_options", "sample_options"]}
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


# ---------- auth-aware derivation (one-flow, complex-case: auth + roles) ----------

def _notes_contract():
    from devagent.schema import Contract
    return Contract(id="openapi_notes", kind="openapi", producer="api", spec={
        "paths": {
            "/auth/register": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["username", "password"],
                    "properties": {"username": {"type": "string"},
                                   "password": {"type": "string"}}}}}},
                "responses": {"201": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"id": {"type": "integer"}}}}}}}}},
            "/auth/login": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["username", "password"],
                    "properties": {"username": {"type": "string"},
                                   "password": {"type": "string"}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"token": {"type": "string"}}}}}}}}},
            "/notes": {
                "get": {"security": [{"bearerAuth": []}],
                        "responses": {"200": {"content": {"application/json": {"schema": {
                            "type": "array", "items": {"type": "object", "properties": {
                                "id": {"type": "integer"}, "text": {"type": "string"}}}}}}}}},
                "post": {"security": [{"bearerAuth": []}],
                         "requestBody": {"content": {"application/json": {"schema": {
                             "type": "object", "required": ["text"],
                             "properties": {"text": {"type": "string"}}}}}},
                         "responses": {"201": {"content": {"application/json": {"schema": {
                             "type": "object", "properties": {"id": {"type": "integer"}}}}}}}}},
            "/admin/users": {
                "get": {"security": [{"bearerAuth": []}], "x-required-role": "admin",
                        "responses": {"200": {"content": {"application/json": {"schema": {
                            "type": "array", "items": {"type": "object", "properties": {
                                "id": {"type": "integer"}}}}}}}}}},
        }})


def test_auth_flow_from_contract_synthesizes_register_login():
    from devagent.contract_utils import auth_flow_from_contract
    flow = auth_flow_from_contract(_notes_contract())
    assert flow["login_route"] == "/auth/login"
    assert flow["register_route"] == "/auth/register"
    assert flow["token_json_path"] == "token"
    assert flow["mode"] == "bearer"
    # runner-prefixed creds so they never collide with the derived register CHECK's sample user
    assert flow["login_body"]["username"].startswith("runner")
    assert flow["register_body"]["username"] == flow["login_body"]["username"]
    assert flow["register_body"]["password"] == flow["login_body"]["password"]


def test_auth_flow_from_contract_none_without_login_path():
    from devagent.contract_utils import auth_flow_from_contract
    assert auth_flow_from_contract(_polls_contract()) is None


def test_derive_checks_marks_protected_ops_auth_and_probes_401():
    from devagent.contract_utils import derive_checks
    checks = derive_checks(_notes_contract())
    by_key = {(c["kind"], c.get("method", "GET"), c["route"], c.get("expected_status", 200)): c
              for c in checks}
    # protected GET: an unauthenticated 401 probe AND an authenticated 200 probe
    assert by_key[("route_status", "GET", "/notes", 401)].get("auth") is not True
    assert by_key[("route_status", "GET", "/notes", 200)]["auth"] is True
    # protected POST + its shape re-read carry auth
    assert by_key[("api_json", "POST", "/notes", 200)]["auth"] is True
    assert by_key[("api_json", "GET", "/notes", 200)]["auth"] is True
    assert by_key[("api_json", "GET", "/notes", 200)]["json_path"] == "0.id"
    # public auth endpoints stay unauthenticated
    assert by_key[("api_json", "POST", "/auth/register", 200)].get("auth") is not True
    # role-gated op: only the member-gets-403 probe; no 200 assertion (no admin cred to derive)
    assert by_key[("route_status", "GET", "/admin/users", 403)]["auth"] is True
    assert ("route_status", "GET", "/admin/users", 200) not in by_key
    assert ("api_json", "GET", "/admin/users", 200) not in by_key


def test_derive_persistence_check_carries_auth():
    from devagent.contract_utils import derive_persistence_check
    p = derive_persistence_check(_notes_contract())
    assert p["route"] == "/notes" and p["auth"] is True


def test_derive_integration_checks_skips_protected_ops():
    from devagent.schema import ServiceNode, SystemDesign
    from devagent.contract_utils import derive_integration_checks
    design = SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="x", provides=["openapi_notes"])],
        contracts=[_notes_contract()])
    routes = [(c.method, c.route) for c in derive_integration_checks(design)]
    # IntegrationRunner has no auth flow support: only public ops cross the wire
    assert ("POST", "/auth/register") in routes and ("POST", "/auth/login") in routes
    assert not any(r.startswith("/notes") or r.startswith("/admin") for _, r in routes)


# ---------- $ref resolution + derivation resilience (live-run findings, 2026-07-06) ----------

def _ref_contract():
    """Architect-realistic spec: schemas under components, referenced via $ref — the deriver
    must resolve them (live run: unresolved $refs made auth underivable, every protected
    check was stripped, and the register check went out body-less -> 400)."""
    from devagent.schema import Contract
    return Contract(id="openapi_notes", kind="openapi", producer="api", spec={
        "components": {"schemas": {
            "LoginRequest": {"type": "object", "required": ["username", "password"],
                             "properties": {"username": {"type": "string"},
                                            "password": {"type": "string"}}},
            "TokenResponse": {"type": "object", "properties": {"token": {"type": "string"}}},
            "Note": {"type": "object", "properties": {"id": {"type": "integer"},
                                                      "text": {"type": "string"}}},
        }},
        "paths": {
            "/auth/login": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/LoginRequest"}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/TokenResponse"}}}}}}},
            "/notes": {"get": {"security": [{"bearerAuth": []}],
                       "responses": {"200": {"content": {"application/json": {"schema": {
                           "type": "array",
                           "items": {"$ref": "#/components/schemas/Note"}}}}}}}},
        }})


def test_auth_flow_resolves_component_refs():
    from devagent.contract_utils import auth_flow_from_contract
    flow = auth_flow_from_contract(_ref_contract())
    assert flow is not None
    assert flow["token_json_path"] == "token"
    assert set(flow["login_body"]) == {"username", "password"}


def test_derive_checks_resolves_refs_in_response_schemas():
    from devagent.contract_utils import derive_checks
    checks = derive_checks(_ref_contract())
    protected_get = [c for c in checks if c["route"] == "/notes" and c.get("auth")]
    assert protected_get and protected_get[0]["expected_status"] == 200


def test_auth_flow_defaults_creds_when_login_has_no_request_schema():
    # A schema-less login op is still derivable: default username/password creds — the build
    # prompt's auth-contract line makes the builder accept exactly these fields.
    from devagent.schema import Contract
    from devagent.contract_utils import auth_flow_from_contract
    c = Contract(id="x", kind="openapi", producer="api", spec={"paths": {
        "/auth/login": {"post": {"responses": {"200": {"content": {"application/json": {
            "schema": {"type": "object", "properties": {"token": {"type": "string"}}}}}}}}}}})
    flow = auth_flow_from_contract(c)
    assert flow is not None
    assert set(flow["login_body"]) == {"username", "password"}


def test_sample_strings_contain_no_spaces():
    # "sample username" 400s against alphanumeric-username validators; every derived string
    # must be validator-safe.
    from devagent.contract_utils import sample_body
    body = sample_body({"type": "object", "required": ["username", "title"], "properties": {
        "username": {"type": "string"}, "title": {"type": "string"}}})
    assert all(" " not in v for v in body.values())
