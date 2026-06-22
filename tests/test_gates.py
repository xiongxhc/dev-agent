from devagent.gates import ContainerExitZero
from devagent.phases.base import PhaseResult


def test_passes_on_exit_zero():
    gr = ContainerExitZero().check(PhaseResult("noop", exit_code=0, output="ok"))
    assert gr.ok
    assert gr.reason == ""


def test_fails_on_nonzero_with_reason():
    gr = ContainerExitZero().check(PhaseResult("noop", exit_code=1, output="boom"))
    assert not gr.ok
    assert "exited 1" in gr.reason
