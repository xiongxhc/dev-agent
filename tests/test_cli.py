from devagent import cli
from devagent.phases.noop import NoopPhase


class FakeSandbox:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cmd):
        return 0, NoopPhase.MARKER + "\n", ""


def test_cli_run_succeeds_with_fake_sandbox(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))
    rc = cli.main(["run", "examples/hello.md"],
                  build_sandbox=lambda run_dir, cfg, ledger: FakeSandbox())
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("succeeded")

    ledgers = list(tmp_path.glob("run-*/ledger.jsonl"))
    assert len(ledgers) == 1
    text = ledgers[0].read_text()
    assert '"status": "succeeded"' in text
    assert '"event": "input"' in text
