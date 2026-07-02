import json

from devagent.deploy import wire_targets
from devagent.schema import ArtifactSpec


def _targets():
    return [
        ArtifactSpec(type="datastore", stack="postgres", name="db"),
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     detail={"datastore": "db"}),
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web"),
    ]


def test_wire_targets_no_prefix_matches_legacy_calls(tmp_path):
    """Pre-M21 5-arg fakes (and DeployPhase behavior) must keep working unchanged."""
    (tmp_path / "web" / "dist").mkdir(parents=True)
    def ss(t, image=None, network=None):
        return "devagent-preview-db"
    def st(workdir, t, image=None, network=None, env=None):
        return f"http://{t.name}:1"
    wired = wire_targets(_targets(), str(tmp_path), network="n",
                         start_target_fn=st, start_service_fn=ss)
    assert wired.urls == {"api": "http://api:1", "web": "http://web:1"}
    assert wired.services == {"db": "devagent-preview-db"}
    assert wired.failed == []
    assert wired.primary_url == "http://web:1"      # frontend-first, DeployPhase's rule
    assert wired.containers == ["devagent-preview-db", "devagent-preview-api",
                                "devagent-preview-web"]


def test_wire_targets_injects_conn_env_from_detail(tmp_path):
    (tmp_path / "web" / "dist").mkdir(parents=True)
    seen = {}
    def st(workdir, t, image=None, network=None, env=None):
        seen[t.name] = env
        return f"http://{t.name}:1"
    wire_targets(_targets(), str(tmp_path), network="n",
                 start_target_fn=st, start_service_fn=lambda *a, **k: "c")
    assert seen["api"] == {
        "DATABASE_URL": "postgresql://devagent:devagent@db:5432/app"}


def test_wire_targets_prefix_namespaces_aliases_containers_and_conn_host(tmp_path):
    (tmp_path / "web" / "dist").mkdir(parents=True)
    seen = {"service": [], "target": []}
    def ss(t, image=None, network=None, alias=None, container_name=None):
        seen["service"].append((t.name, alias, container_name))
        return container_name
    def st(workdir, t, image=None, network=None, env=None, alias=None, container_name=None):
        seen["target"].append((t.name, alias, container_name, env))
        return f"http://{t.name}:1"
    wired = wire_targets(_targets(), str(tmp_path), network="n", alias_prefix="apinode-",
                         start_target_fn=st, start_service_fn=ss)
    assert seen["service"] == [("db", "apinode-db", "devagent-preview-apinode-db")]
    name, alias, cname, env = seen["target"][0]
    assert (name, alias, cname) == ("api", "apinode-api", "devagent-preview-apinode-api")
    assert env["DATABASE_URL"] == "postgresql://devagent:devagent@apinode-db:5432/app"
    assert wired.containers == ["devagent-preview-apinode-db", "devagent-preview-apinode-api",
                                "devagent-preview-apinode-web"]


def test_wire_targets_extra_env_seeds_backend_and_detail_wins(tmp_path):
    seen = {}
    def st(workdir, t, image=None, network=None, env=None):
        seen[t.name] = env
        return "http://api:1"
    targets = [ArtifactSpec(type="backend", stack="node-express", name="api")]
    wire_targets(targets, str(tmp_path), network="n",
                 extra_env={"DATABASE_URL": "postgresql://devagent:devagent@dbnode:5432/app"},
                 start_target_fn=st, start_service_fn=lambda *a, **k: None)
    assert seen["api"] == {"DATABASE_URL": "postgresql://devagent:devagent@dbnode:5432/app"}


def test_wire_targets_frontend_api_base_when_no_internal_backend(tmp_path):
    (tmp_path / "web" / "dist").mkdir(parents=True)
    def st(workdir, t, image=None, network=None, env=None):
        return "http://web:1"
    wire_targets([ArtifactSpec(type="frontend", stack="node-vite-react", name="web")],
                 str(tmp_path), frontend_api_base="http://apinode:9",
                 start_target_fn=st, start_service_fn=lambda *a, **k: None)
    cfg = json.loads((tmp_path / "web" / "dist" / "config.json").read_text())
    assert cfg == {"apiBase": "http://apinode:9"}
