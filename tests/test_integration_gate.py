from devagent.phases.base import PhaseResult
from devagent.integration import IntegrationReport
from devagent.phase_gates import IntegrationGate


def _result(report, exit_code=0):
    return PhaseResult(name="integration", exit_code=exit_code, output_artifact=report)


def test_passes_when_all_steps_ok():
    rep = IntegrationReport(steps=[{"service": "api", "route": "/x", "ok": True, "detail": ""}])
    assert IntegrationGate().check(_result(rep)).ok


def test_fails_and_names_failed_steps():
    rep = IntegrationReport(steps=[
        {"service": "api", "route": "/x", "ok": True, "detail": ""},
        {"service": "web", "route": "/", "ok": False, "detail": "404"}])
    r = IntegrationGate().check(_result(rep))
    assert not r.ok and "web" in r.reason and "/" in r.reason


def test_fails_on_empty_report():
    assert not IntegrationGate().check(_result(IntegrationReport(steps=[]))).ok


def test_precheck_rejects_wrong_artifact():
    assert not IntegrationGate().check(PhaseResult(name="integration", exit_code=1,
                                                   output_artifact=None)).ok
