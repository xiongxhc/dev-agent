import pytest
from devagent import recipes


def test_registry_has_the_two_m6_recipes():
    assert recipes.is_registered("node-vite-react")
    assert recipes.is_registered("node-express")
    assert not recipes.is_registered("java-springboot")  # M7+


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        recipes.get("nope")


def test_frontend_recipe_shape():
    r = recipes.get("node-vite-react")
    assert r.type == "frontend"
    assert r.boot is None                       # static
    assert "dist/index.html" in r.artifact_glob
    assert "route_status" in r.supported_checks


def test_backend_recipe_boots_and_checks_api():
    r = recipes.get("node-express")
    assert r.type == "backend"
    assert r.boot is not None
    assert r.boot.health_path == "/health"
    assert "api_json" in r.supported_checks
    assert r.toolchain.image == "devagent-sandbox:m2"
