"""Runs INSIDE the M2 image, after a successful rebuild. Per-target dispatch:

  - backend targets:  boots the service (using _boot cmd/port/health_path from scope.json),
                      polls until healthy, then runs acceptance checks against the live HTTP port.
  - frontend targets: serves the static build dir (dist/ or _static_dir) with an SPA-fallback
                      HTTP handler, then runs acceptance checks against that server.

Check kinds dispatched per target:
  - route_status      HTTP GET the route, assert status == expected_status   (no browser)
  - selector_present  Playwright renders the route, assert the CSS selector exists (browser)
  - api_json          HTTP method + optional body, assert a dotted JSON path equals a value
  - command_exit /    Run an argv in the target workdir, assert exit code and optional stdout
    stdout_matches    regex pattern.

Only selector_present needs a browser; Playwright is imported LAZILY so a route-only spec
never requires it, and this module imports — and its HTTP path unit-tests — without
Playwright installed. Writes .devagent/acceptance.json.
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


def check_persistence_survives_restart(base_url, route, method, body, json_path,
                                       verify_route, restart) -> dict:
    """Prove durable state: write a record, restart the APP (the datastore, if any, stays up),
    then read it back. `restart` is a callable returning the new base_url (same port) or None
    if the app did not become healthy again."""
    kind = "persistence_survives_restart"
    # 1. write
    write_url = base_url.rstrip("/") + route
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(write_url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            created = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"kind": kind, "route": route, "ok": False, "detail": f"write failed: {e}"}
    found, created_id = _dig(created, json_path)
    if not found:
        return {"kind": kind, "route": route, "ok": False,
                "detail": f"created id not at json_path {json_path!r}"}
    # 2. restart the app (datastore stays up)
    new_base = restart()
    if not new_base:
        return {"kind": kind, "route": route, "ok": False,
                "detail": "app did not become healthy after restart"}
    # 3. read back — the created id must still be present
    read_url = new_base.rstrip("/") + verify_route
    try:
        with urllib.request.urlopen(read_url, timeout=10) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"kind": kind, "route": route, "ok": False, "detail": f"read-back failed: {e}"}
    present = str(created_id) in json.dumps(payload)
    return {"kind": kind, "route": route, "ok": present,
            "detail": f"id {created_id!r} {'present' if present else 'missing'} after restart"}


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


def run_target_checks(target: dict, base_url: str, workdir: str, restart=None) -> list[dict]:
    """Dispatch each check in *target* by kind against *base_url* (HTTP) or *workdir* (command).
    `restart` (set only for booted backends) lets a persistence check bounce the app."""
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
        elif k == "persistence_survives_restart":
            if restart is None:
                out.append({"kind": k, "ok": False,
                            "detail": "persistence check requires a bootable backend"})
            else:
                out.append(check_persistence_survives_restart(
                    base_url, c["route"], c.get("method", "POST"), c.get("body"),
                    c.get("json_path"), c.get("verify_route"), restart))
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
            env = {**os.environ, "PORT": str(boot["port"])}
            health_url = f"http://127.0.0.1:{boot['port']}{boot.get('health_path', '/health')}"
            proc_cell = [subprocess.Popen(boot["cmd"], cwd=workdir, env=env)]
            if not _poll_http(health_url):
                proc_cell[0].terminate()
                try:
                    proc_cell[0].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc_cell[0].kill()
                all_results.append({
                    "kind": "boot", "target": name, "ok": False,
                    "detail": f"service did not become healthy at {health_url}",
                })
                continue
            base_url = f"http://127.0.0.1:{boot['port']}"

            def _restart(_proc_cell=proc_cell, _env=env, _cmd=boot["cmd"], _wd=workdir,
                         _health=health_url, _base=base_url):
                p = _proc_cell[0]
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                _proc_cell[0] = subprocess.Popen(_cmd, cwd=_wd, env=_env)
                return _base if _poll_http(_health) else None

            try:
                results = run_target_checks(target, base_url, workdir, restart=_restart)
            except Exception as e:  # one target's failure must not abort the others
                all_results.append({"kind": "target_error", "target": target.get("name"),
                                    "ok": False, "detail": str(e)})
                continue
            finally:
                proc_cell[0].terminate()
                try:
                    proc_cell[0].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc_cell[0].kill()
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
                    time.sleep(0.05)
            try:
                results = run_target_checks(target, base_url, workdir)
            except Exception as e:  # one target's failure must not abort the others
                all_results.append({"kind": "target_error", "target": target.get("name"), "ok": False, "detail": str(e)})
                continue
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
