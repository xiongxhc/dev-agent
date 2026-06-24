"""Egress containment, end-to-end through real Docker: the allowlist proxy permits
Anthropic + npm, denies everything else, and the --internal network blocks all direct
egress. Needs the M2 image (skips if absent). Marked `docker` so it skips without a daemon."""

import subprocess

import pytest

from devagent import egress

pytestmark = pytest.mark.docker


def _m2_present() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", egress.DEFAULT_IMAGE],
                              capture_output=True).returncode == 0
    except Exception:
        return False


_PROBE = r"""
import socket
def via_proxy(h):
    s = socket.create_connection(("devagent-proxy", 3128), timeout=10)
    s.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s\r\n\r\n" % (h, h)).encode())
    line = s.recv(120).decode("latin1").split("\r\n")[0]; s.close(); return line
def direct(h):
    try:
        socket.create_connection((h, 443), timeout=6).close(); return "LEAK"
    except Exception:
        return "blocked"
print("ALLOW", via_proxy("api.anthropic.com"))
print("DENY", via_proxy("example.com"))
print("DIRECT", direct("example.com"))
"""


@pytest.mark.skipif(not _m2_present(), reason=f"{egress.DEFAULT_IMAGE} image not built")
def test_proxy_allows_anthropic_blocks_others_and_no_direct_egress():
    net, _ = egress.ensure()
    out = subprocess.run(
        ["docker", "run", "--rm", "--network", net, "--user", "1000:1000",
         egress.DEFAULT_IMAGE, "python3", "-c", _PROBE],
        capture_output=True, text=True,
    ).stdout
    assert "ALLOW HTTP/1.1 200" in out      # api.anthropic.com permitted
    assert "DENY HTTP/1.1 403" in out       # example.com refused by the allowlist
    assert "DIRECT blocked" in out          # --internal network => no direct route out
