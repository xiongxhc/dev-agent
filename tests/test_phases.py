from devagent.phases.base import PhaseContext
from devagent.phases.noop import NoopPhase


class FakeSandbox:
    def __init__(self):
        self.calls = []

    def run(self, cmd):
        self.calls.append(cmd)
        return 0, NoopPhase.MARKER + "\n", ""


def test_noop_execs_marker_in_sandbox():
    sb = FakeSandbox()
    ctx = PhaseContext(sandbox=sb, budget=None, ledger=None)
    result = NoopPhase().run(ctx)
    assert result.exit_code == 0
    assert result.output == NoopPhase.MARKER
    assert sb.calls == [["echo", NoopPhase.MARKER]]
