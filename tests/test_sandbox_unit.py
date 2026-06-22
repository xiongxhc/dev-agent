"""Non-Docker unit tests for Sandbox wiring (no daemon needed)."""

from pathlib import Path

from devagent.sandbox import Sandbox


def test_out_dir_is_absolute_even_for_relative_run_dir():
    # Regression: a relative -v source makes Docker treat it as a named volume.
    sb = Sandbox(Path("runs/run-xyz"), image="devagent-sandbox:m1")
    assert sb.out_dir.is_absolute()
    assert sb.out_dir.name == "out"
