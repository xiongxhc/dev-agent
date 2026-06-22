"""Containment is the whole point of M1, so these are the load-bearing tests.
They prove the CAUSE of isolation (no host bind mount but /out, read-only rootfs, only a
loopback interface), not just an outcome that could pass for the wrong reason.
All require a Docker daemon (auto-skipped unless DEVAGENT_REQUIRE_DOCKER=1)."""

import subprocess

import pytest

from devagent.ledger import Ledger
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


def test_fs_containment_only_out_mounted_and_rootfs_readonly(tmp_path, sandbox_image):
    """Prove the CAUSE: /out is the only non-virtual mount, rootfs is read-only, and a
    host secret is absent — not merely that one nonexistent path can't be read."""
    secret = tmp_path / "secret.txt"
    secret.write_text("topsecret")
    run_dir = tmp_path / "r"
    with Sandbox(run_dir, image=sandbox_image) as sb:
        c_secret, o_secret, _ = sb.run(["cat", str(secret)])
        c_root, _, _ = sb.run(["sh", "-c", "echo x > /etc/passwd 2>/dev/null"])
        c_out, _, _ = sb.run(["sh", "-c", "echo ok > /out/f"])
        _, mounts, _ = sb.run(["cat", "/proc/mounts"])

    assert c_secret != 0 and "topsecret" not in o_secret  # host path not in namespace
    assert c_root != 0                                     # rootfs read-only
    assert (run_dir / "out" / "f").read_text().strip() == "ok"  # /out is the writable mount

    lines = [ln.split() for ln in mounts.splitlines() if ln.strip()]
    root = next(ln for ln in lines if ln[1] == "/")
    assert "ro" in root[3].split(","), "rootfs must be mounted read-only"
    assert any(ln[1] == "/out" for ln in lines), "/out must be present as the only host mount"


def test_network_none_has_no_route_out_and_no_ethernet(tmp_path, sandbox_image):
    """Prove --network none structurally: no default route and no ethernet interface.
    (Checked via /proc/net/* — /sys/class/net is unreliable as it lists kernel pseudo
    tunnel devices present in every namespace regardless of networking.)"""
    with Sandbox(tmp_path / "r", image=sandbox_image) as sb:
        _, route, _ = sb.run(["cat", "/proc/net/route"])
        _, dev, _ = sb.run(["cat", "/proc/net/dev"])
    data = [ln.split() for ln in route.splitlines()[1:] if ln.strip()]
    # a default route would have Destination 00000000 (and reach a gateway) — none here
    assert not any(cols[1] == "00000000" for cols in data), "no default route expected"
    assert "eth0" not in dev, "no ethernet interface expected under --network none"


def test_sandbox_start_recorded_to_ledger(tmp_path, sandbox_image):
    led = Ledger(tmp_path / "r")
    with Sandbox(tmp_path / "r", image=sandbox_image, ledger=led):
        pass
    events = led.events()
    kinds = [e["event"] for e in events]
    assert "sandbox_start" in kinds and "sandbox_stop" in kinds
    start = next(e for e in events if e["event"] == "sandbox_start")
    assert "--network" in start["argv"] and "none" in start["argv"]
    assert start["image_digest"].startswith("sha256:")
