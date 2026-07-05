"""The Executor seam + a FakeExecutor proving the contract (no SDK needed)."""

from devagent.executor import BuildRequest, BuildResult, Executor, enrich_scope
from devagent.schema import AcceptanceCheck, ArtifactSpec, Plan, ProjectScope, Task


class FakeExecutor:
    """Stand-in build engine for harness tests — returns a canned BuildResult."""

    def build(self, req: BuildRequest) -> BuildResult:
        return BuildResult(repo_path=req.workdir, success=True, tokens_in=10, tokens_out=5)


def _req(tmp_path):
    scope = ProjectScope(
        title="Hello",
        targets=[ArtifactSpec(
            type="frontend", stack="node-vite-react", name="web", detail={},
            acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")],
        )],
    )
    plan = Plan(tasks=[Task(id="a", description="page", owned_files=["src/App.tsx"])])
    return BuildRequest(scope=scope, plan=plan, workdir=str(tmp_path), run_id="run-1")


def test_fake_executor_satisfies_protocol(tmp_path):
    ex: Executor = FakeExecutor()
    result = ex.build(_req(tmp_path))
    assert result.repo_path == str(tmp_path)
    assert result.success is True
    assert result.tokens_in == 10


def test_build_request_is_frozen(tmp_path):
    import dataclasses
    req = _req(tmp_path)
    try:
        req.run_id = "mutated"  # frozen dataclass -> should raise
        assert False, "BuildRequest must be frozen"
    except dataclasses.FrozenInstanceError:
        pass


def test_enrich_scope_tags_kind_and_handles_service_targets():
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     detail={"datastore": "db", "conn_env": "DATABASE_URL"},
                     acceptance_checks=[]),
        ArtifactSpec(type="datastore", stack="postgres", name="db", acceptance_checks=[]),
    ])
    enriched = enrich_scope(scope)
    by_name = {t["name"]: t for t in enriched["targets"]}
    assert by_name["api"]["kind"] == "build"
    assert by_name["db"]["kind"] == "service"
    assert by_name["db"]["_boot"] is None        # service has no boot


def test_enrich_scope_carries_target_auth_flow():
    # regression: the in-container acceptance runner reads enrich_scope()'s output, so a
    # target's auth flow MUST survive serialization or auth:true checks fail "no auth flow".
    from devagent.schema import AuthFlow
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     auth=AuthFlow(login_route="/auth/login", token_json_path="token",
                                   login_body={"username": "u", "password": "p"}),
                     acceptance_checks=[AcceptanceCheck(kind="api_json", route="/todos", auth=True)]),
    ])
    api = {t["name"]: t for t in enrich_scope(scope)["targets"]}["api"]
    assert api["auth"]["login_route"] == "/auth/login"
    assert api["auth"]["token_json_path"] == "token"
    assert api["acceptance_checks"][0]["auth"] is True


def test_broadcast_consumed_empty_is_none():
    from devagent.executor import broadcast_consumed
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api")])
    assert broadcast_consumed(scope, ()) is None


def test_broadcast_consumed_maps_every_target():
    from devagent.executor import broadcast_consumed
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api"),
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web")])
    spec = {"paths": {"/api/todos": {"get": {}}}}
    m = broadcast_consumed(scope, (spec,))
    assert m == {"api": [spec], "web": [spec]}


def test_enrich_scope_drops_route_status_contradicting_provided_contract():
    # Live-run finding (2026-07-03, run #4): scope emitted a route_status (a GET probe,
    # expecting 200) against the contract's POST-only vote route; the correctly-built api
    # 404'd it and the repair loop burned both repairs on an unsatisfiable check. The frozen
    # contract is the authority: drop success-expecting route_status checks whose path the
    # provided openapi declares GET-less.
    from devagent.schema import AcceptanceCheck
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api", acceptance_checks=[
            AcceptanceCheck(kind="route_status", route="/polls"),                        # GET exists -> keep
            AcceptanceCheck(kind="route_status", route="/polls/1/options/1/vote"),       # POST-only -> drop
            AcceptanceCheck(kind="route_status", route="/polls/1/options/1/vote",
                            expected_status=404),                                        # failure-assert -> keep
            AcceptanceCheck(kind="api_json", route="/polls/1/options/1/vote",
                            method="POST", json_path="votes"),                           # right way -> keep
        ])])
    contract = {"paths": {
        "/polls": {"get": {}, "post": {}},
        "/polls/{pollId}/options/{optionId}/vote": {"post": {}},
    }}
    baked = enrich_scope(scope, provided_by_target={"api": [contract]})
    kept = [(c["kind"], c["route"], c.get("expected_status")) for c in baked["targets"][0]["acceptance_checks"]]
    assert ("route_status", "/polls", 200) in kept
    assert ("route_status", "/polls/1/options/1/vote", 200) not in kept
    assert ("route_status", "/polls/1/options/1/vote", 404) in kept
    assert ("api_json", "/polls/1/options/1/vote", 200) in kept


def test_enrich_scope_keeps_all_checks_without_provided_contract():
    from devagent.schema import AcceptanceCheck
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api", acceptance_checks=[
            AcceptanceCheck(kind="route_status", route="/anything")])])
    baked = enrich_scope(scope)
    assert len(baked["targets"][0]["acceptance_checks"]) == 1


def test_enrich_scope_drops_api_json_check_contradicting_provided_array_contract():
    # Live-run finding (2026-07-03, Team Polls, 4/4 system-build runs): the scope model wrote
    # an api_json check assuming GET /polls returns an object ({"polls": [...]}) while the
    # frozen openapi contract declares the 200 response as a bare array. No build can satisfy
    # both: enrich_scope's OWN api_json checks (object-shaped) and the system-level integration
    # checks (array-shaped, "0.question") disagree, so the LLM built a hybrid
    # {"length", "count", "polls", "data"} object to limp past both — which broke the frontend,
    # which correctly implements the contract's array shape and renders nothing for anything
    # that isn't Array.isArray(). The contract is the authority: drop api_json checks whose
    # json_path's root-level assumption (object key vs array index) the provided openapi
    # contradicts.
    from devagent.schema import AcceptanceCheck
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api", acceptance_checks=[
            AcceptanceCheck(kind="api_json", route="/polls", method="GET",
                            json_path="polls"),               # object-shaped -> contradicts array -> drop
            AcceptanceCheck(kind="api_json", route="/polls", method="GET",
                            json_path="0.question"),           # array-indexed -> matches array -> keep
            AcceptanceCheck(kind="api_json", route="/polls", method="POST",
                            json_path="id"),                   # POST has no declared schema -> keep
        ])])
    contract = {"paths": {
        "/polls": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"type": "array", "items": {"type": "object"}}}}}}},
                   "post": {}},
    }}
    baked = enrich_scope(scope, provided_by_target={"api": [contract]})
    kept = [(c["kind"], c["route"], c["json_path"]) for c in baked["targets"][0]["acceptance_checks"]]
    assert ("api_json", "/polls", "polls") not in kept
    assert ("api_json", "/polls", "0.question") in kept
    assert ("api_json", "/polls", "id") in kept


def test_enrich_scope_drops_get_probe_expecting_created_status():
    # Feishu live run (2026-07-03): scope emitted route_status /auth/register want 201 —
    # route_status probes GET, and a GET can never return 201 Created; 12/13 checks passed
    # and both repairs burned on this one. No contract needed to know it's wrong.
    from devagent.schema import AcceptanceCheck
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api", acceptance_checks=[
            AcceptanceCheck(kind="route_status", route="/auth/register", expected_status=201),
            AcceptanceCheck(kind="route_status", route="/health"),
            AcceptanceCheck(kind="api_json", route="/auth/register", method="POST",
                            json_path="id"),
        ])])
    baked = enrich_scope(scope)
    kept = [(c["kind"], c["route"]) for c in baked["targets"][0]["acceptance_checks"]]
    assert ("route_status", "/auth/register") not in kept
    assert ("route_status", "/health") in kept
    assert ("api_json", "/auth/register") in kept
