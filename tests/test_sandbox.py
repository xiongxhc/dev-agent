"""Containment is the whole point of M1, so these are the load-bearing tests.
All require a Docker daemon (auto-skipped otherwise)."""

import subprocess

import pytest

from devagent.sandbox import Sandbox

pytestmark = pytest.mark.docker


def test_run_exec_returns_output(tmp_path, sandbox_image):
    with Sandbox(tmp_path / "r", image=sandbox_image) as sb:
        code, out, err = sb.run(["echo", "hi"])
    assert code == 0
    assert out.strip() == "hi"


def test_container_removed_after_exit(tmp_path, sandbox_image):
    sb = Sandbox(tmp_path / "r", image=sandbox_image)
    with sb:
        cid = sb.cid
    left = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"id={cid}"],
                          capture_output=True, text=True)
    assert left.stdout.strip() == ""  # --rm proved


def test_fs_containment_host_file_unreadable(tmp_path, sandbox_image):
    secret = tmp_path / "secret.txt"
    secret.write_text("topsecret")
    with Sandbox(tmp_path / "r", image=sandbox_image) as sb:
        code, out, err = sb.run(["cat", str(secret)])
    assert code != 0
    assert "topsecret" not in out


def test_out_dir_is_the_only_shared_rw_mount(tmp_path, sandbox_image):
    run_dir = tmp_path / "r"
    with Sandbox(run_dir, image=sandbox_image) as sb:
        code, _, err = sb.run(["sh", "-c", "echo built > /out/artifact.txt"])
        assert code == 0, err
    assert (run_dir / "out" / "artifact.txt").read_text().strip() == "built"


def test_egress_blocked_with_network_none(tmp_path, sandbox_image):
    with Sandbox(tmp_path / "r", image=sandbox_image) as sb:
        code, out, err = sb.run(["wget", "-T", "3", "-q", "-O", "-", "http://1.1.1.1"])
    assert code != 0  # no network interface -> cannot reach the internet


def test_sandbox_start_recorded_to_ledger(tmp_path, sandbox_image):
    from devagent.ledger import Ledger
    led = Ledger(tmp_path / "r")
    with Sandbox(tmp_path / "r", image=sandbox_image, ledger=led):
        pass
    kinds = [e["event"] for e in led.events()]
    assert "sandbox_start" in kinds and "sandbox_stop" in kinds
    start = next(e for e in led.events() if e["event"] == "sandbox_start")
    assert "--network" in start["argv"] and "none" in start["argv"]
