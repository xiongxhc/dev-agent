"""A minimal allowlisting forward proxy — the egress chokepoint for contained build/verify.

Runs inside the M2 image (python3 only, so NO extra image to pull). Handles HTTPS via the
CONNECT method: it opens a tunnel ONLY to allowlisted hosts and denies everything else
(deny-by-default — a parse failure or unknown host is refused, never allowed). Build/verify
containers run on an --internal Docker network with no direct route to the internet and
HTTPS_PROXY pointed here, so this proxy is their only way out.

Allowlist defaults to Anthropic + the npm registry; override via DEVAGENT_EGRESS_ALLOW
(comma-separated host suffixes; a leading dot matches subdomains)."""

import os
import select
import socket
import sys
import threading

DEFAULT_ALLOW = ["api.anthropic.com", ".anthropic.com", "registry.npmjs.org", ".npmjs.org"]
PORT = 3128


def host_allowed(host: str, allow: list[str]) -> bool:
    """Deny-by-default suffix match. '.example.com' matches example.com and any subdomain."""
    host = (host or "").lower().strip().rstrip(".")
    if not host:
        return False
    for a in allow:
        a = a.lower().strip()
        if not a:
            continue
        if a.startswith("."):
            if host == a[1:] or host.endswith(a):
                return True
        elif host == a:
            return True
    return False


def _tunnel(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


def handle(conn: socket.socket, allow: list[str]) -> None:
    upstream = None
    try:
        conn.settimeout(30)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                return
            req += chunk
            if len(req) > 65536:
                return
        line = req.split(b"\r\n", 1)[0].decode("latin1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")  # only https/CONNECT
            return
        host, _, port = parts[1].partition(":")
        if not host_allowed(host, allow):
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        upstream = socket.create_connection((host, int(port or 443)), timeout=15)
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        conn.settimeout(None)
        _tunnel(conn, upstream)
    except Exception:  # noqa: BLE001 — any failure denies; never leak a tunnel
        try:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except OSError:
            pass
    finally:
        if upstream is not None:
            upstream.close()
        conn.close()


def main() -> None:
    env = os.getenv("DEVAGENT_EGRESS_ALLOW")
    allow = [h for h in (env.split(",") if env else DEFAULT_ALLOW) if h.strip()]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(64)
    sys.stderr.write(f"egress proxy listening :{PORT}, allow={allow}\n")
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, allow), daemon=True).start()


if __name__ == "__main__":
    main()
