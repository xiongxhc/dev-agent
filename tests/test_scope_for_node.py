"""One-flow (2026-07-03): the SystemDesign is the ONLY scope authority. scope_for_node
derives each sub-build's ProjectScope mechanically — no second LLM decision, no re-invented
targets or checks (the live-run failure class: sub-run scope contradicted the frozen contract)."""

from devagent.phase_gates import ScopeGate
from devagent.phases.base import PhaseResult
from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.system_build import scope_for_node


def _design():
    return SystemDesign(title="Team Polls", services=[
        ServiceNode(id="db", name="db", kind="datastore", stack="postgres", prd_slice="store",
                    provides=["db_schema_polls"]),
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="REST API for polls", depends_on=["db"],
                    provides=["openapi_polls"], consumes=["db_schema_polls"]),
        ServiceNode(id="web", name="web", kind="frontend", stack="node-vite-react",
                    prd_slice="React UI", depends_on=["api"], consumes=["openapi_polls"])],
        contracts=[
            Contract(id="db_schema_polls", kind="db_schema", producer="db",
                     spec={"tables": {"polls": {}}}),
            Contract(id="openapi_polls", kind="openapi", producer="api", spec={"paths": {
                "/polls": {
                    "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "array", "items": {"type": "object",
                                                   "properties": {"id": {"type": "integer"}}}}}}}}},
                    "post": {"responses": {"201": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"id": {"type": "integer"}}}}}}}}}}})])


def _node(design, node_id):
    return next(s for s in design.services if s.id == node_id)


def test_backend_scope_carries_design_datastore_and_derived_checks():
    design = _design()
    scope = scope_for_node(_node(design, "api"), design)
    by_name = {t.name: t for t in scope.targets}
    assert set(by_name) == {"db", "api"}                 # the DESIGN's db, never a re-invented one
    assert by_name["db"].stack == "postgres" and not by_name["db"].acceptance_checks
    api = by_name["api"]
    assert api.detail["description"] == "REST API for polls"
    assert api.detail["datastore"] == "db" and api.detail["conn_env"] == "DATABASE_URL"
    kinds = [c.kind for c in api.acceptance_checks]
    assert "persistence_survives_restart" in kinds       # datastore-backed -> durability derived
    shape = [c for c in api.acceptance_checks if c.kind == "api_json" and c.method == "GET"]
    assert shape and shape[0].json_path == "0.id"        # array root straight from the contract


def test_frontend_scope_is_single_target_with_root_probe():
    design = _design()
    scope = scope_for_node(_node(design, "web"), design)
    assert [t.name for t in scope.targets] == ["web"]    # api dep is NOT a target (it's consumed)
    checks = scope.targets[0].acceptance_checks
    assert [(c.kind, c.route) for c in checks] == [("route_status", "/")]


def test_derived_scopes_pass_scope_gate():
    design = _design()
    for sid in ("api", "web"):
        scope = scope_for_node(_node(design, sid), design)
        res = ScopeGate().check(PhaseResult(name="scope", exit_code=0, output_artifact=scope))
        assert res.ok, f"{sid}: {res.reason}"


def _auth_design():
    return SystemDesign(title="Private Notes", services=[
        ServiceNode(id="db", name="db", kind="datastore", stack="postgres", prd_slice="store",
                    provides=["db_schema_notes"]),
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="notes API with auth", depends_on=["db"],
                    provides=["openapi_notes"], consumes=["db_schema_notes"])],
        contracts=[
            Contract(id="db_schema_notes", kind="db_schema", producer="db", spec={}),
            Contract(id="openapi_notes", kind="openapi", producer="api", spec={"paths": {
                "/auth/register": {"post": {"requestBody": {"content": {"application/json": {
                    "schema": {"type": "object", "required": ["username", "password"],
                               "properties": {"username": {"type": "string"},
                                              "password": {"type": "string"}}}}}}}},
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
                                    "id": {"type": "integer"}}}}}}}}},
                    "post": {"security": [{"bearerAuth": []}],
                             "responses": {"201": {"content": {"application/json": {"schema": {
                                 "type": "object",
                                 "properties": {"id": {"type": "integer"}}}}}}}}}}})])


def test_auth_backend_scope_carries_synthesized_flow_and_gated_checks():
    design = _auth_design()
    scope = scope_for_node(_node(design, "api"), design)
    api = {t.name: t for t in scope.targets}["api"]
    assert api.auth is not None and api.auth.login_route == "/auth/login"
    assert api.auth.register_route == "/auth/register"
    assert api.auth.token_json_path == "token"
    protected = [c for c in api.acceptance_checks if c.auth]
    unauth_probe = [c for c in api.acceptance_checks
                    if c.kind == "route_status" and c.expected_status == 401]
    assert protected and unauth_probe                    # both sides of the auth gate
    res = ScopeGate().check(PhaseResult(name="scope", exit_code=0, output_artifact=scope))
    assert res.ok, res.reason


def test_frontend_scope_derives_mobile_fit_when_prd_slice_says_mobile():
    design = _design()
    web = _node(design, "web")
    web_mobile = web.model_copy(update={
        "prd_slice": "React SPA optimised for mobile WebView embedding; touch-friendly."})
    design_m = design.model_copy(update={
        "services": [s if s.id != "web" else web_mobile for s in design.services]})
    scope = scope_for_node(web_mobile, design_m)
    kinds = [(c.kind, c.route) for c in scope.targets[0].acceptance_checks]
    assert ("mobile_fit", "/") in kinds and ("route_status", "/") in kinds
    # a non-mobile slice derives no mobile_fit
    plain = scope_for_node(_node(design, "web"), design)
    assert all(c.kind != "mobile_fit" for c in plain.targets[0].acceptance_checks)
