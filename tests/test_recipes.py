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


def test_frontend_hint_enforces_design_floor():
    # the frontend recipe is the ONLY design-guidance injection point (the build runs the Agent
    # SDK with setting_sources=[], so host skills never reach it). Guard the key directives.
    hint = recipes.get("node-vite-react").scaffold_hint.lower()
    assert "contrast" in hint                       # the white-on-white-button class of bug
    assert "white-on-white" in hint or "default" in hint
    assert "focus" in hint                          # visible keyboard focus
    assert "reduced-motion" in hint or "prefers-reduced-motion" in hint
    assert "ai-default" in hint or "templated" in hint   # anti AI-smell


def test_backend_hint_covers_persistence():
    hint = recipes.get("node-express").scaffold_hint.lower()
    assert "database_url" in hint or "conn" in hint
    assert "sqlite" in hint
    assert "idempotent" in hint or "create table if not exists" in hint
    assert "pin" in hint                         # driver version pinned like every dep


# --- M11: declarative recipes (add a stack as DATA, not a code edit) ---
import json
from devagent.recipes import load_external_recipes, recipe_from_dict


def test_recipe_from_dict_builds_a_bootable_backend():
    r = recipe_from_dict({
        "name": "python-flask", "type": "backend", "toolchain": {"image": "devagent-sandbox:m2"},
        "scaffold_hint": "Scaffold a Flask app", "build_cmd": "pip install -r requirements.txt",
        "artifact_glob": "app.py",
        "boot": {"cmd": ["python", "app.py"], "port": 5000, "health_path": "/healthz"},
        "supported_checks": ["api_json", "route_status"],
    })
    assert r.name == "python-flask" and r.type == "backend"
    assert r.boot.cmd == ("python", "app.py") and r.boot.port == 5000
    assert r.boot.health_path == "/healthz"
    assert r.supported_checks == ("api_json", "route_status")
    assert r.kind == "build" and r.service is None


def test_recipe_from_dict_builds_a_service_with_dict_env():
    r = recipe_from_dict({
        "name": "redis", "type": "datastore", "kind": "service",
        "toolchain": {"image": "devagent-sandbox:m2"},
        "service": {"image": "redis:7", "port": 6379, "env": {"FOO": "bar"},
                    "volume_path": "/data", "ready_cmd": ["redis-cli", "ping"],
                    "conn_url_template": "redis://{host}:{port}"},
    })
    assert r.kind == "service" and r.service.image == "redis:7"
    assert r.service.env == (("FOO", "bar"),)        # JSON object env normalized to pairs
    assert r.service.ready_cmd == ("redis-cli", "ping")


def test_load_external_recipes_from_dir(tmp_path):
    (tmp_path / "rust.json").write_text(json.dumps({
        "name": "rust-axum", "type": "backend", "toolchain": {"image": "devagent-rust:m1"},
        "build_cmd": "cargo build --release", "artifact_glob": "target/release/app",
        "boot": {"cmd": ["./target/release/app"], "port": 8080}, "supported_checks": ["api_json"],
    }))
    loaded = load_external_recipes(tmp_path)
    assert "rust-axum" in loaded
    assert loaded["rust-axum"].toolchain.image == "devagent-rust:m1"
    assert loaded["rust-axum"].boot.health_path == "/health"   # defaulted


def test_external_manifest_can_override_a_builtin(tmp_path):
    (tmp_path / "override.json").write_text(json.dumps({
        "name": "node-express", "type": "backend", "toolchain": {"image": "my-custom:latest"},
        "build_cmd": "pnpm build", "artifact_glob": "dist/server.js",
    }))
    loaded = load_external_recipes(tmp_path)
    assert loaded["node-express"].toolchain.image == "my-custom:latest"


def test_a_file_may_hold_a_list_of_recipes(tmp_path):
    (tmp_path / "pair.json").write_text(json.dumps([
        {"name": "a", "type": "backend", "toolchain": {"image": "i"}, "artifact_glob": "x"},
        {"name": "b", "type": "frontend", "toolchain": {"image": "i"}, "artifact_glob": "y"},
    ]))
    loaded = load_external_recipes(tmp_path)
    assert set(loaded) == {"a", "b"}


def test_malformed_manifest_fails_loudly_with_filename(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"type": "backend"}))  # missing name + toolchain
    with pytest.raises(ValueError) as ei:
        load_external_recipes(tmp_path)
    assert "bad.json" in str(ei.value)


def test_missing_dir_is_empty_not_an_error(tmp_path):
    assert load_external_recipes(tmp_path / "nope") == {}


# --- M11: declarative TOOLCHAIN images (build a new toolchain from a manifest) ---
from devagent.recipes import toolchains


def test_recipe_from_dict_parses_toolchain_dockerfile():
    r = recipe_from_dict({
        "name": "java-springboot", "type": "backend",
        "toolchain": {"image": "devagent-jdk:m1", "dockerfile": "Dockerfile.jdk"},
        "build_cmd": "mvn package", "artifact_glob": "target/app.jar",
    })
    assert r.toolchain.image == "devagent-jdk:m1"
    assert r.toolchain.dockerfile == "Dockerfile.jdk"


def test_toolchain_build_specs_maps_manifests_to_docker_builds(tmp_path):
    (tmp_path / "Dockerfile.jdk").write_text("FROM eclipse-temurin:21\n")
    (tmp_path / "java.json").write_text(json.dumps({
        "name": "java-springboot", "type": "backend", "build_cmd": "mvn package",
        "artifact_glob": "target/app.jar",
        "toolchain": {"image": "devagent-jdk:m1", "dockerfile": "Dockerfile.jdk"},
    }))
    specs = toolchains.toolchain_build_specs(tmp_path)
    assert len(specs) == 1
    assert specs[0]["image"] == "devagent-jdk:m1"
    assert specs[0]["dockerfile"] == str((tmp_path / "Dockerfile.jdk").resolve())
    assert specs[0]["context"] == str(tmp_path.resolve())   # defaults to the Dockerfile's dir


def test_prebuilt_toolchains_contribute_no_build_spec(tmp_path):
    # node recipes use the bundled m2 image (no dockerfile) → nothing to build
    (tmp_path / "flask.json").write_text(json.dumps({
        "name": "python-flask", "type": "backend", "build_cmd": "pip install -r requirements.txt",
        "artifact_glob": "app.py", "toolchain": {"image": "devagent-sandbox:m2"},
    }))
    assert toolchains.toolchain_build_specs(tmp_path) == []


def test_build_all_runs_docker_build_per_declared_image(tmp_path):
    (tmp_path / "Dockerfile.rust").write_text("FROM rust:1\n")
    (tmp_path / "rust.json").write_text(json.dumps({
        "name": "rust-axum", "type": "backend", "build_cmd": "cargo build", "artifact_glob": "app",
        "toolchain": {"image": "devagent-rust:m1", "dockerfile": "Dockerfile.rust"},
    }))
    calls = []
    built = toolchains.build_all(tmp_path, runner=lambda argv, **kw: calls.append(argv))
    assert built == ["devagent-rust:m1"]
    assert calls[0][:3] == ["docker", "build", "-f"]
    assert "devagent-rust:m1" in calls[0]


def test_one_dockerfile_backing_many_recipes_builds_once(tmp_path):
    (tmp_path / "Dockerfile.jvm").write_text("FROM eclipse-temurin:21\n")
    (tmp_path / "jvm.json").write_text(json.dumps([
        {"name": "java-spring", "type": "backend", "build_cmd": "mvn package", "artifact_glob": "a.jar",
         "toolchain": {"image": "devagent-jvm:m1", "dockerfile": "Dockerfile.jvm"}},
        {"name": "kotlin-ktor", "type": "backend", "build_cmd": "gradle build", "artifact_glob": "b.jar",
         "toolchain": {"image": "devagent-jvm:m1", "dockerfile": "Dockerfile.jvm"}},
    ]))
    specs = toolchains.toolchain_build_specs(tmp_path)
    assert [s["image"] for s in specs] == ["devagent-jvm:m1"]   # deduped by image
