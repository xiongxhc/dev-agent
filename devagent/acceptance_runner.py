"""Runs INSIDE the M2 image, after a successful rebuild. Boots a static server on the
built dist/ and runs the spec's acceptance_checks, KIND-DISPATCHED:

  - route_status      HTTP GET the route, assert status == expected_status   (no browser)
  - selector_present  Playwright renders the route, assert the CSS selector exists (browser)

Only selector_present needs a browser; Playwright is imported LAZILY so a route-only spec
(and the future backend/CLI check kinds) never require it, and this module imports — and
its HTTP path unit-tests — without Playwright installed. Writes .devagent/acceptance.json.
"""

import functools
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path("/out")
DEV = OUT / ".devagent"
DIST = OUT / "dist"


def check_route_status(base_url: str, route: str, expected_status: int) -> dict:
    url = base_url.rstrip("/") + route
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001 — a dead server is a failed check, not a crash
        return {"kind": "route_status", "route": route, "ok": False, "detail": f"request failed: {e}"}
    return {"kind": "route_status", "route": route, "ok": code == expected_status,
            "detail": f"status {code} (want {expected_status})"}


def check_selector_present(base_url: str, route: str, selector: str) -> dict:
    from playwright.sync_api import sync_playwright  # lazy: only when a selector check exists

    url = base_url.rstrip("/") + route
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            found = page.query_selector(selector) is not None
        finally:
            browser.close()
    return {"kind": "selector_present", "route": route, "ok": found,
            "detail": f"selector {selector!r} {'found' if found else 'missing'}"}


def _dig(obj, dotted: str):
    """Walk a dotted path ('items.0.id') into nested dicts/lists. Returns (found, value)."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False, None
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def check_api_json(base_url, route, method, body, json_path, json_equals) -> dict:
    url = base_url.rstrip("/") + route
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 — any failure is a failed check, not a crash
        return {"kind": "api_json", "route": route, "ok": False, "detail": f"request failed: {e}"}
    found, value = _dig(payload, json_path) if json_path else (True, payload)
    if not found:
        return {"kind": "api_json", "route": route, "ok": False, "detail": f"path {json_path!r} absent"}
    ok = True if json_equals is None else (value == json_equals)
    return {"kind": "api_json", "route": route, "ok": ok,
            "detail": f"{json_path}={value!r}" + ("" if json_equals is None else f" (want {json_equals!r})")}


def check_command(workdir, argv, expected_exit, pattern) -> dict:
    try:
        proc = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return {"kind": "command", "argv": argv, "ok": False, "detail": f"run failed: {e}"}
    ok = proc.returncode == expected_exit
    if ok and pattern is not None:
        ok = re.search(pattern, proc.stdout) is not None
    return {"kind": "command", "argv": argv, "ok": ok,
            "detail": f"exit {proc.returncode} (want {expected_exit})"
                      + ("" if pattern is None else f"; /{pattern}/ {'matched' if ok else 'no match'}")}


def run_checks(checks: list[dict], base_url: str) -> list[dict]:
    """Dispatch each check by kind. Unknown kinds fail loudly rather than passing silently."""
    results = []
    for c in checks:
        kind = c.get("kind")
        if kind == "route_status":
            results.append(check_route_status(base_url, c["route"], c.get("expected_status", 200)))
        elif kind == "selector_present":
            results.append(check_selector_present(base_url, c["route"], c["selector"]))
        else:
            results.append({"kind": kind, "route": c.get("route"), "ok": False,
                            "detail": f"unsupported check kind {kind!r}"})
    return results


class _SPAHandler(http.server.SimpleHTTPRequestHandler):
    """Serves dist/, falling back to index.html for client-routed paths (so SPA routes 200)."""

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, *a):  # quiet
        pass


def _serve(directory: Path) -> tuple[socketserver.TCPServer, str]:
    handler = functools.partial(_SPAHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def run_target_checks(target: dict, base_url: str, workdir: str) -> list[dict]:
    """Dispatch each check in *target* by kind against *base_url* (HTTP) or *workdir* (command)."""
    out = []
    for c in target.get("acceptance_checks", []):
        k = c["kind"]
        if k == "route_status":
            out.append(check_route_status(base_url, c["route"], c.get("expected_status", 200)))
        elif k == "selector_present":
            out.append(check_selector_present(base_url, c["route"], c["selector"]))
        elif k == "api_json":
            out.append(check_api_json(base_url, c["route"], c.get("method", "GET"),
                                      c.get("body"), c.get("json_path"), c.get("json_equals")))
        elif k in ("command_exit", "stdout_matches"):
            out.append(check_command(workdir, c["argv"], c.get("expected_exit", 0), c.get("pattern")))
        else:
            out.append({"kind": k, "ok": False, "detail": f"unsupported kind {k!r}"})
    for r in out:
        r["target"] = target.get("name")
    return out


def _poll_http(url: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Poll *url* until it responds 2xx/3xx or *timeout* seconds elapse. Returns success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 400:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


def main() -> None:
    scope = json.loads((DEV / "scope.json").read_text())
    targets = scope.get("targets", [])

    all_results: list[dict] = []

    for target in targets:
        name = target["name"]
        workdir = str(OUT / name)
        boot = target.get("_boot")          # {"cmd": [...], "port": N, "health_path": "/health"} or null
        static_dir = target.get("_static_dir")  # relative subpath under /out/<name>, e.g. "dist"

        if boot:
            # Boot the service in the target directory with PORT env var set
            env = {**os.environ, "PORT": str(boot["port"])}
            proc = subprocess.Popen(boot["cmd"], cwd=workdir, env=env)
            health_url = f"http://127.0.0.1:{boot['port']}{boot.get('health_path', '/health')}"
            ready = _poll_http(health_url)
            if not ready:
                proc.terminate()
                proc.wait(timeout=5)
                all_results.append({
                    "kind": "boot", "target": name, "ok": False,
                    "detail": f"service did not become healthy at {health_url}",
                })
                continue
            base_url = f"http://127.0.0.1:{boot['port']}"
            try:
                results = run_target_checks(target, base_url, workdir)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        else:
            # Static frontend — serve dist (or _static_dir) with the SPA handler
            serve_path = Path(workdir) / (static_dir or "dist")
            httpd, base_url = _serve(serve_path)
            # wait for the listener socket
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=0.2).close()
                    break
                except OSError:
                    pass
            try:
                results = run_target_checks(target, base_url, workdir)
            finally:
                httpd.shutdown()

        all_results.extend(results)

    DEV.mkdir(parents=True, exist_ok=True)
    (DEV / "acceptance.json").write_text(json.dumps({
        "checks": all_results,
        "all_pass": bool(all_results) and all(r["ok"] for r in all_results),
    }))


if __name__ == "__main__":
    main()
