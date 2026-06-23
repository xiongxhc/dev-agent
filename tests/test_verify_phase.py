"""VerifyPhase (wraps a verifier) + VerifyGate (pure check on the report)."""

from devagent.budget import Budget
from devagent.ledger import Ledger
from devagent.phase_gates import VerifyGate
from devagent.phases.base import PhaseContext, PhaseResult
from devagent.phases.verify import VerifyPhase
from devagent.verifier import VerifyReport, VerifyRequest


def _ctx(tmp_path):
    return PhaseContext(sandbox=None, budget=Budget(10**9, 1e9, 9), ledger=Ledger(tmp_path / "run"))


class FakeVerifier:
    def __init__(self, report):
        self.report = report
        self.seen: VerifyRequest | None = None

    def verify(self, req):
        self.seen = req
        return self.report


def test_verify_phase_calls_verifier_with_workdir_and_wraps_report(tmp_path):
    rep = VerifyReport(build_ok=True, dist_present=True, exit_code=0)
    fv = FakeVerifier(rep)
    result = VerifyPhase(verifier=fv, workdir="/x/out", run_id="r9").run(_ctx(tmp_path))
    assert fv.seen.workdir == "/x/out" and fv.seen.run_id == "r9"
    assert result.name == "verify"
    assert result.exit_code == 0
    assert result.output_artifact is rep


def test_verify_phase_exit_1_when_build_not_ok(tmp_path):
    rep = VerifyReport(build_ok=False, dist_present=False, exit_code=1, log_tail="boom")
    result = VerifyPhase(verifier=FakeVerifier(rep), workdir="/x", run_id="r").run(_ctx(tmp_path))
    assert result.exit_code == 1


def test_verify_gate_passes_when_build_ok_and_dist_present():
    res = PhaseResult("verify", 0, output_artifact=VerifyReport(build_ok=True, dist_present=True, exit_code=0))
    assert VerifyGate().check(res).ok is True


def test_verify_gate_fails_when_dist_missing_despite_zero_exit():
    res = PhaseResult("verify", 0, output_artifact=VerifyReport(build_ok=True, dist_present=False, exit_code=0))
    gr = VerifyGate().check(res)
    assert gr.ok is False
    assert "dist" in gr.reason


def test_verify_gate_fails_when_no_artifact():
    assert VerifyGate().check(PhaseResult("verify", 1, output="verifier crashed")).ok is False
