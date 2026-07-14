"""Pure token-accounting from the SDK's cumulative usage dict. No SDK/container needed —
the shape below is a REAL claude_agent_sdk ResultMessage.usage captured from a live probe."""

from devagent.sdk_runner import input_output_tokens

# Real ResultMessage.usage from a trivial "say hi" run — note cache_read dwarfs input.
REAL_USAGE = {
    "input_tokens": 2341,
    "cache_creation_input_tokens": 183,
    "cache_read_input_tokens": 15246,
    "output_tokens": 4,
    "service_tier": "standard",
}


def test_input_includes_cache_create_and_read():
    tin, tout = input_output_tokens(REAL_USAGE)
    assert tin == 2341 + 183 + 15246   # the cache tokens the old code dropped
    assert tout == 4


def test_handles_missing_and_none_fields():
    assert input_output_tokens({"input_tokens": 10, "output_tokens": 2}) == (10, 2)
    assert input_output_tokens({"cache_read_input_tokens": None, "input_tokens": 5}) == (5, 0)


def test_none_usage_is_zero():
    assert input_output_tokens(None) == (0, 0)
    assert input_output_tokens("not a dict") == (0, 0)


def test_prompt_covers_each_target_with_its_recipe_hint():
    from devagent.sdk_runner import build_prompt

    scope = {"title": "App", "targets": [
        {"type": "frontend", "stack": "node-vite-react", "name": "web", "detail": {"pages": ["/"]},
         "_scaffold_hint": "Scaffold a Vite", "build_cmd": "pnpm build", "artifact_glob": "dist/index.html"},
        {"type": "backend", "stack": "node-express", "name": "api", "detail": {"endpoints": ["/api/x"]},
         "_scaffold_hint": "Scaffold an Express", "build_cmd": "pnpm build", "artifact_glob": "dist/server.js"},
    ]}
    plan = {"tasks": [{"id": "t1", "description": "d", "owned_files": ["web/src/App.tsx"]}]}
    p = build_prompt(scope, plan)
    assert "web/" in p and "api/" in p
    assert "Scaffold a Vite" in p and "Scaffold an Express" in p
    assert "dist/index.html" in p
    assert "dist/server.js" in p


def test_context_prefix_selects_update_over_repair(tmp_path):
    from devagent.sdk_runner import context_prefix, REPAIR_PREFIX, UPDATE_PREFIX
    assert context_prefix(tmp_path) == ""
    (tmp_path / "repair.txt").write_text("DIAG")
    assert context_prefix(tmp_path) == REPAIR_PREFIX.format(diagnostics="DIAG")
    (tmp_path / "update.txt").write_text("make the button blue")
    out = context_prefix(tmp_path)
    assert out == UPDATE_PREFIX.format(change="make the button blue")
    assert "UPDATE pass" in out and "FAILED" not in out   # feature work isn't framed as a fix


def test_prompt_surfaces_auth_contract_and_checks_to_the_builder():
    from devagent.sdk_runner import build_prompt

    scope = {"title": "App", "targets": [{
        "type": "backend", "stack": "node-express", "name": "api", "detail": {},
        "_scaffold_hint": "Scaffold an Express", "build_cmd": "pnpm build", "artifact_glob": "dist/server.js",
        "auth": {"login_route": "/auth/login", "login_body": {"username": "testuser", "password": "pw"},
                 "register_route": "/auth/register", "register_body": {"username": "testuser", "password": "pw"},
                 "token_json_path": "token", "header": "Authorization", "scheme": "Bearer"},
        "acceptance_checks": [{"kind": "api_json", "route": "/todos", "auth": True}],
    }]}
    plan = {"tasks": [{"id": "t1", "description": "d", "owned_files": ["api/src/index.ts"]}]}
    p = build_prompt(scope, plan)
    # the builder must see the exact credentials + token path the verifier will use
    assert "AUTH CONTRACT" in p
    assert "/auth/login" in p and "/auth/register" in p
    assert "testuser" in p and "token" in p
    assert "do NOT" in p and "email" in p          # the explicit no-extra-fields guard
    assert '"route": "/todos"' in p                # acceptance checks surfaced too
