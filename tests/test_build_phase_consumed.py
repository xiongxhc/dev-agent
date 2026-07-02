from devagent.executor import BuildResult
from devagent.phases.base import PhaseContext
from devagent.phases.build import BuildPhase


class _Budget:
    def add_tokens(self, n): pass
    def add_cost(self, usd): pass


def _run_with(consumed_kw):
    seen = {}
    class _Ex:
        def build(self, req):
            seen["consumed"] = req.consumed_contracts
            return BuildResult(repo_path="x", success=True)
    phase = BuildPhase(executor=_Ex(), workdir="/tmp/w", run_id="r", **consumed_kw)
    ctx = PhaseContext(sandbox=None, budget=_Budget(), ledger=None,
                       artifacts={"scope": object(), "plan": object()})
    res = phase.run(ctx)
    return res, seen


def test_build_phase_stamps_consumed_contracts_on_request():
    res, seen = _run_with({"consumed_contracts": ({"paths": {}},)})
    assert res.exit_code == 0
    assert seen["consumed"] == ({"paths": {}},)


def test_build_phase_default_no_consumed_contracts():
    _, seen = _run_with({})
    assert seen["consumed"] == ()
