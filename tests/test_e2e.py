"""End-to-end: the real CLI against the real Docker sandbox."""

import pytest

from devagent import cli

pytestmark = pytest.mark.docker


def test_cli_run_against_real_sandbox(tmp_path, monkeypatch, sandbox_image, capsys):
    monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("DEVAGENT_IMAGE", sandbox_image)
    rc = cli.main(["run", "examples/hello.md"])
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("succeeded")

    ledgers = list(tmp_path.glob("run-*/ledger.jsonl"))
    assert len(ledgers) == 1
    text = ledgers[0].read_text()
    assert '"status": "succeeded"' in text
    assert '"event": "sandbox_start"' in text
