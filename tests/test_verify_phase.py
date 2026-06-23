"""VerifyGate — the trusted check on a VerifyReport. (The rebuild-from-source + repair loop
now lives inside BuildPhase; see test_repair_loop.py. This file covers the gate.)"""

from devagent.phase_gates import VerifyGate
from devagent.phases.base import PhaseResult
from devagent.verifier import VerifyReport


def test_verify_gate_passes_when_build_ok_and_dist_present():
    res = PhaseResult("build", 0, output_artifact=VerifyReport(build_ok=True, dist_present=True, exit_code=0))
    assert VerifyGate().check(res).ok is True


def test_verify_gate_fails_when_dist_missing_despite_zero_exit():
    res = PhaseResult("build", 0, output_artifact=VerifyReport(build_ok=True, dist_present=False, exit_code=0))
    gr = VerifyGate().check(res)
    assert gr.ok is False
    assert "dist" in gr.reason


def test_verify_gate_fails_when_build_not_ok():
    # exit_code 0 so we exercise the gate's own build_ok branch, not the exit precheck.
    res = PhaseResult("build", 0, output_artifact=VerifyReport(build_ok=False, dist_present=True, exit_code=1))
    gr = VerifyGate().check(res)
    assert gr.ok is False
    assert "rebuild from source failed" in gr.reason


def test_verify_gate_fails_when_no_artifact():
    assert VerifyGate().check(PhaseResult("build", 1, output="verifier crashed")).ok is False
