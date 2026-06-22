"""Non-Docker unit tests for Sandbox wiring (no daemon needed)."""

import subprocess
from pathlib import Path

from devagent.sandbox import Sandbox


def test_out_dir_is_absolute_even_for_relative_run_dir():
    # Regression: a relative -v source makes Docker treat it as a named volume.
    sb = Sandbox(Path("runs/run-xyz"), image="devagent-sandbox:m1")
    assert sb.out_dir.is_absolute()
    assert sb.out_dir.name == "out"


def test_run_timeout_returns_nonzero_not_crash(monkeypatch):
    # A hung command must surface as a failed command, never an uncaught TimeoutExpired.
    sb = Sandbox(Path("runs/run-xyz"), image="img", exec_timeout=1)
    sb.cid = "fakecid"

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker exec", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    code, out, err = sb.run(["sleep", "999"])
    assert code == 124
    assert "timed out" in err.lower()
