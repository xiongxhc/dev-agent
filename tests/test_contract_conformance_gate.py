from devagent.phases.base import PhaseResult
from devagent.verifier import VerifyReport
from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.phase_gates import ContractConformanceGate


def _ok_report():
    return VerifyReport(build_ok=True, dist_present=True, exit_code=0)


def _result(art=None):
    return PhaseResult(name="verify", exit_code=0, output_artifact=art or _ok_report())


def _design():
    return SystemDesign(
        title="t",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="x", provides=["api.openapi"])],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}}}})],
    )


def test_passes_when_provided_contract_conforms():
    gate = ContractConformanceGate(
        design=_design(), base_urls={"api": "http://x"},
        check_route_status=lambda *a, **k: {"ok": True})
    assert gate.check(_result()).ok


def test_fails_when_a_contract_route_missing():
    gate = ContractConformanceGate(
        design=_design(), base_urls={"api": "http://x"},
        check_route_status=lambda *a, **k: {"ok": False, "detail": "404"})
    r = gate.check(_result())
    assert not r.ok and "/api/todos" in r.reason


def test_fails_when_producer_has_no_base_url():
    gate = ContractConformanceGate(
        design=_design(), base_urls={},
        check_route_status=lambda *a, **k: {"ok": True})
    assert not gate.check(_result()).ok


def test_precheck_rejects_wrong_artifact():
    gate = ContractConformanceGate(design=_design(), base_urls={"api": "http://x"})
    assert not gate.check(PhaseResult(name="verify", exit_code=1, output_artifact=None)).ok
