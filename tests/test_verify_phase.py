"""VerifyGate — the trusted check on a VerifyReport (rebuild-from-source + acceptance). The
rebuild + repair loop now lives inside BuildPhase; see test_repair_loop.py."""

from devagent.phase_gates import VerifyGate
from devagent.phases.base import PhaseResult
from devagent.verifier import CheckResult, VerifyReport

_PASS = [CheckResult("route_status", "/", True, "status 200")]


def _res(report):
    return PhaseResult("build", 0 if report.ok else 1, output_artifact=report)


def test_verify_gate_passes_when_build_ok_and_checks_pass():
    rep = VerifyReport(build_ok=True, dist_present=True, exit_code=0, checks=_PASS)
    assert VerifyGate().check(_res(rep)).ok is True


def test_verify_gate_fails_when_dist_missing_despite_zero_exit():
    rep = VerifyReport(build_ok=True, dist_present=False, exit_code=0, checks=_PASS)
    # exit_code 0 in the PhaseResult so we hit the gate's own dist branch, not the precheck.
    gr = VerifyGate().check(PhaseResult("build", 0, output_artifact=rep))
    assert gr.ok is False and "artifact" in gr.reason


def test_verify_gate_fails_when_build_not_ok():
    rep = VerifyReport(build_ok=False, dist_present=True, exit_code=1, checks=[])
    gr = VerifyGate().check(PhaseResult("build", 0, output_artifact=rep))
    assert gr.ok is False and "rebuild from source failed" in gr.reason


def test_verify_gate_fails_when_an_acceptance_check_fails():
    rep = VerifyReport(build_ok=True, dist_present=True, exit_code=0,
                       checks=[CheckResult("selector_present", "/", False, "selector '#hero' missing")])
    gr = VerifyGate().check(PhaseResult("build", 0, output_artifact=rep))
    assert gr.ok is False and "acceptance checks failed" in gr.reason


def test_verify_gate_fails_when_no_checks_ran():
    rep = VerifyReport(build_ok=True, dist_present=True, exit_code=0, checks=[])
    gr = VerifyGate().check(PhaseResult("build", 0, output_artifact=rep))
    assert gr.ok is False and "no acceptance checks" in gr.reason


def test_verify_gate_fails_when_no_artifact():
    assert VerifyGate().check(PhaseResult("build", 1, output="verifier crashed")).ok is False
