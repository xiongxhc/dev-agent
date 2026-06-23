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
import socket
import socketserver
import threading
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


def main() -> None:
    spec = json.loads((DEV / "spec.json").read_text())
    httpd, base_url = _serve(DIST)
    try:
        # wait for the listener
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=0.2).close()
                break
            except OSError:
                pass
        results = run_checks(spec.get("acceptance_checks", []), base_url)
    finally:
        httpd.shutdown()
    DEV.mkdir(parents=True, exist_ok=True)
    (DEV / "acceptance.json").write_text(json.dumps({
        "checks": results,
        "all_pass": bool(results) and all(r["ok"] for r in results),
    }))


if __name__ == "__main__":
    main()
