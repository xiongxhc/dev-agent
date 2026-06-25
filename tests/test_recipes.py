import pytest
from devagent import recipes
from devagent.recipes.base import Recipe, ServiceSpec, Toolchain


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


def test_servicespec_is_frozen_and_carries_run_info():
    s = ServiceSpec(
        image="postgres:16-alpine", port=5432,
        env=(("POSTGRES_USER", "devagent"),),
        volume_path="/var/lib/postgresql/data",
        ready_cmd=("pg_isready", "-U", "devagent"),
        conn_url_template="postgresql://devagent:devagent@{host}:{port}/app",
    )
    assert s.port == 5432 and s.ready_timeout_s == 60.0
    with pytest.raises(Exception):       # frozen — no mutation
        s.port = 1


def test_recipe_defaults_to_build_kind_and_allows_service():
    build = Recipe(name="x", type="frontend", toolchain=Toolchain(image="img"),
                   scaffold_hint="h", build_cmd="b", artifact_glob="dist/x", boot=None)
    assert build.kind == "build" and build.service is None
    svc = Recipe(name="pg", type="datastore", toolchain=Toolchain(image="img"),
                 kind="service",
                 service=ServiceSpec(image="postgres:16-alpine", port=5432, env=(),
                                     volume_path="/v", ready_cmd=("true",),
                                     conn_url_template="postgresql://{host}:{port}/app"))
    assert svc.kind == "service" and svc.boot is None and svc.build_cmd == ""


def test_postgres_service_recipe_registered():
    r = recipes.get("postgres")
    assert r.kind == "service" and r.type == "datastore"
    assert r.boot is None and r.supported_checks == ()
    assert r.service.image == "postgres:16-alpine" and r.service.port == 5432
    assert "{host}" in r.service.conn_url_template and "{port}" in r.service.conn_url_template


def test_mongo_service_recipe_registered():
    r = recipes.get("mongo")
    assert r.kind == "service" and r.service.image == "mongo:7"
    assert r.service.port == 27017


def test_node_express_supports_persistence_check():
    assert "persistence_survives_restart" in recipes.get("node-express").supported_checks


def test_backend_hint_covers_persistence():
    hint = recipes.get("node-express").scaffold_hint.lower()
    assert "database_url" in hint or "conn" in hint
    assert "sqlite" in hint
    assert "idempotent" in hint or "create table if not exists" in hint
    assert "pin" in hint                         # driver version pinned like every dep
