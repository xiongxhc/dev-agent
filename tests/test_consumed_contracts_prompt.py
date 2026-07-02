from devagent.schema import AcceptanceCheck, ArtifactSpec, ProjectScope
from devagent.executor import enrich_scope
from devagent.sdk_runner import build_prompt


def _scope():
    return ProjectScope(title="Todo", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web", detail={"pages": ["/"]},
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")]),
    ])


def test_enrich_scope_stamps_consumed_contracts():
    baked = enrich_scope(_scope(), consumed_by_target={"web": [{"paths": {"/api/todos": {"get": {}}}}]})
    assert baked["targets"][0]["_consumed_contracts"] == [{"paths": {"/api/todos": {"get": {}}}}]


def test_enrich_scope_default_no_consumed_contracts():
    baked = enrich_scope(_scope())
    assert baked["targets"][0]["_consumed_contracts"] == []


def test_build_prompt_renders_consumed_block_when_present():
    baked = enrich_scope(_scope(), consumed_by_target={"web": [{"paths": {"/api/x": {"get": {}}}}]})
    text = build_prompt(baked, {"tasks": []})
    assert "CONSUMED CONTRACTS" in text and "/api/x" in text


def test_build_prompt_omits_consumed_block_when_absent():
    text = build_prompt(enrich_scope(_scope()), {"tasks": []})
    assert "CONSUMED CONTRACTS" not in text
