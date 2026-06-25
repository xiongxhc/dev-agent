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
