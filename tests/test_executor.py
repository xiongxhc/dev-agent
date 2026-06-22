"""The Executor seam + a FakeExecutor proving the contract (no SDK needed)."""

from devagent.executor import BuildRequest, BuildResult, Executor
from devagent.schema import AcceptanceCheck, Plan, Spec, Task


class FakeExecutor:
    """Stand-in build engine for harness tests — returns a canned BuildResult."""

    def build(self, req: BuildRequest) -> BuildResult:
        return BuildResult(repo_path=req.workdir, success=True, tokens_in=10, tokens_out=5)


def _req(tmp_path):
    spec = Spec(
        title="Hello",
        pages=["/"],
        acceptance_checks=[AcceptanceCheck(kind="route_status", route="/", expected_status=200)],
    )
    plan = Plan(tasks=[Task(id="a", description="page", owned_files=["src/App.tsx"])])
    return BuildRequest(spec=spec, plan=plan, workdir=str(tmp_path), run_id="run-1")


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
