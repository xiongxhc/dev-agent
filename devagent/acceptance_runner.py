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
import http.cookiejar
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


def check_route_status(base_url: str, route: str, expected_status: int, headers=None) -> dict:
    url = base_url.rstrip("/") + route
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001 — a dead server is a failed check, not a crash
        return {"kind": "route_status", "route": route, "ok": False, "detail": f"request failed: {e}"}
    return {"kind": "route_status", "route": route, "ok": code == expected_status,
            "detail": f"status {code} (want {expected_status})"}


def normalize_selector(selector: str) -> str:
    """Rewrite jQuery-style `:contains('text')` to Playwright's `:has-text("text")`. The scope
    model emits the jQuery form despite it not being valid CSS — querySelectorAll throws a
    SyntaxError, so the check errors before evaluating and is unsatisfiable by ANY build (the
    repair loop then burns its repairs on it; deepseek live run, 2026-08-15). Normalizing at
    the evaluation chokepoint also heals frozen scopes from past runs. Valid CSS is untouched."""
    def repl(m):
        return ':has-text("' + m.group(2).replace('"', '\\"') + '")'
    return re.sub(r""":contains\(\s*(['"]?)(.*?)\1\s*\)""", repl, selector)


def check_selector_present(base_url: str, route: str, selector: str) -> dict:
    from playwright.sync_api import sync_playwright  # lazy: only when a selector check exists

    selector = normalize_selector(selector)
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


_MOBILE_VIEWPORT = {"width": 390, "height": 844}    # common phone logical resolution
_TOUCH_TARGET_MIN_PX = 44                            # Apple HIG floor (Material says 48)


def _mobile_fit_verdict(scroll_width: int, inner_width: int, small_targets: list) -> tuple:
    """Pure verdict for a mobile_fit render: (ok, detail). Fails on horizontal overflow
    (a desktop layout squeezed into a phone viewport) or interactive elements under the
    touch-target floor — the two mechanical signatures of 'not designed for mobile'."""
    problems = []
    if scroll_width > inner_width:
        problems.append(f"horizontal overflow: content {scroll_width}px > viewport {inner_width}px")
    if small_targets:
        shown = "; ".join(small_targets[:5])
        problems.append(f"{len(small_targets)} touch target(s) under "
                        f"{_TOUCH_TARGET_MIN_PX}px: {shown}")
    if problems:
        return False, " | ".join(problems)
    return True, (f"no horizontal overflow at {inner_width}px; "
                  f"all touch targets >= {_TOUCH_TARGET_MIN_PX}px")


def check_mobile_fit(base_url: str, route: str) -> dict:
    """Render *route* at a phone viewport and apply _mobile_fit_verdict. Checkboxes/radios are
    exempt (their tap area is the label); plain links are exempt (inline prose links are
    legitimately text-sized) — buttons, inputs, selects, and role=button are the floor."""
    from playwright.sync_api import sync_playwright  # lazy: only when a mobile check exists

    url = base_url.rstrip("/") + route
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport=dict(_MOBILE_VIEWPORT))
            page.goto(url, wait_until="networkidle")
            data = page.evaluate("""() => {
                const sels = 'button, select, textarea, [role="button"], ' +
                             'input:not([type=hidden]):not([type=checkbox]):not([type=radio])';
                const small = [];
                for (const el of document.querySelectorAll(sels)) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;    // hidden
                    if (r.height < MINPX) {
                        const label = (el.innerText || el.value || el.type || el.tagName)
                            .trim().slice(0, 24);
                        small.push(el.tagName.toLowerCase() + " '" + label + "' " +
                                   Math.round(r.height) + "px");
                    }
                }
                return {scrollWidth: document.documentElement.scrollWidth,
                        innerWidth: window.innerWidth, small};
            }""".replace("MINPX", str(_TOUCH_TARGET_MIN_PX)))
        finally:
            browser.close()
    ok, detail = _mobile_fit_verdict(data["scrollWidth"], data["innerWidth"], data["small"])
    return {"kind": "mobile_fit", "route": route, "ok": ok, "detail": detail}


def _dig(obj, dotted: str):
    """Walk a dotted path ('items.0.id') into nested dicts/lists. Returns (found, value).
    A JSONPath-style root prefix ('$.items.0.id', or a bare '$') is tolerated — LLM-emitted
    checks (the architect's IntegrationChecks) write it habitually — by stripping it first."""
    if dotted == "$":
        return True, obj
    if dotted.startswith("$."):
        dotted = dotted[2:]
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


def _contains_value(obj, target) -> bool:
    """True if *target* (compared as a string) equals any SCALAR value anywhere in *obj*.
    Exact-value match (not substring) so a created id can't false-positive against an
    unrelated field that merely contains its digits — this is the durability proof."""
    if isinstance(obj, dict):
        return any(_contains_value(v, target) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_value(v, target) for v in obj)
    return str(obj) == str(target)


def check_api_json(base_url, route, method, body, json_path, json_equals, headers=None) -> dict:
    url = base_url.rstrip("/") + route
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
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
                                       verify_route, restart, headers=None) -> dict:
    """Prove durable state: write a record, restart the APP (the datastore, if any, stays up),
    then read it back. `restart` is a callable returning the new base_url (same port) or None
    if the app did not become healthy again."""
    kind = "persistence_survives_restart"
    # 1. write
    write_url = base_url.rstrip("/") + route
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(write_url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
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
    read_req = urllib.request.Request(read_url, headers=headers or {})
    try:
        with urllib.request.urlopen(read_req, timeout=10) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"kind": kind, "route": route, "ok": False, "detail": f"read-back failed: {e}"}
    present = _contains_value(payload, created_id)
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
        elif kind == "mobile_fit":
            results.append(check_mobile_fit(base_url, c["route"]))
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


def _cred_bearer(login_resp, jar, auth) -> tuple[dict | None, str | None]:
    """Bearer style: read the token from the login JSON body and return the header dict."""
    try:
        payload = json.loads(login_resp.read().decode())
    except Exception as e:  # noqa: BLE001
        return None, f"login response not JSON: {e}"
    found, token = _dig(payload, auth.get("token_json_path", ""))
    if not found or not token:
        return None, f"token not at json_path {auth.get('token_json_path')!r}"
    value = f"{auth.get('scheme', 'Bearer')} {token}".strip()
    return {auth.get("header", "Authorization"): value}, None


def _cred_cookie(login_resp, jar, auth) -> tuple[dict | None, str | None]:
    """Cookie/session style: replay the cookies the cookie jar captured during login as a Cookie
    header. The jar is fed by an HTTPCookieProcessor opener, so cookies set on an intermediate
    redirect (a 302 → dashboard login) are captured too — reading the final response's headers
    alone would miss them."""
    pairs = [f"{c.name}={c.value}" for c in jar]
    if not pairs:
        return None, "no Set-Cookie returned at login"
    return {"Cookie": "; ".join(pairs)}, None


# Auth-style dispatch table — adding a style is an entry here (data-not-code), matching the
# open KNOWN_AUTH_MODES vocabulary in schema.py (M10 depth / M11 declarative auth styles).
_CRED_BUILDERS = {"bearer": _cred_bearer, "cookie": _cred_cookie}


def obtain_auth_header(base_url: str, auth: dict) -> tuple[dict | None, str | None]:
    """Execute an AuthFlow: optional register, then login; build the credential header dict
    per `mode` (bearer token or captured session cookie). Returns (headers, None) on success
    or (None, detail) on failure. Run ONCE per flow before its checks — deterministic, no model.

    Login (and register) go through a per-flow cookie-jar opener so session cookies are captured
    even when login responds with a redirect."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # optional register — best effort: an "already exists" failure is fine, login is the gate.
    if auth.get("register_route"):
        rbody = auth.get("register_body") or auth.get("login_body")
        rdata = json.dumps(rbody).encode() if rbody is not None else None
        rreq = urllib.request.Request(base_url.rstrip("/") + auth["register_route"], data=rdata,
                                      method=auth.get("register_method", "POST"),
                                      headers={"Content-Type": "application/json"})
        try:
            opener.open(rreq, timeout=10).close()
        except Exception:  # noqa: BLE001 — already-registered etc. is not fatal
            pass
    mode = auth.get("mode", "bearer")
    builder = _CRED_BUILDERS.get(mode)
    if builder is None:
        return None, f"unsupported auth mode {mode!r}"
    lbody = auth.get("login_body") or {}
    ldata = json.dumps(lbody).encode() if lbody is not None else None
    lreq = urllib.request.Request(base_url.rstrip("/") + auth["login_route"], data=ldata,
                                  method=auth.get("login_method", "POST"),
                                  headers={"Content-Type": "application/json"})
    try:
        with opener.open(lreq, timeout=10) as r:
            return builder(r, jar, auth)
    except Exception as e:  # noqa: BLE001
        return None, f"login failed: {e}"


def run_target_checks(target: dict, base_url: str, workdir: str, restart=None) -> list[dict]:
    """Dispatch each check in *target* by kind against *base_url* (HTTP) or *workdir* (command).
    `restart` (set only for booted backends) lets a persistence check bounce the app.

    Credentials: the target's default `auth` flow serves `auth=True` checks; each named flow in
    `actors` serves checks that reference it via `as: <name>` (the authz permission matrix). Each
    distinct flow logs in ONCE up front; a login failure fails only the checks that need it."""
    out = []
    creds: dict[str, tuple[dict | None, str | None]] = {}  # "" = default flow; "<name>" = actor
    if target.get("auth"):
        creds[""] = obtain_auth_header(base_url, target["auth"])
    for a in target.get("actors") or []:
        if a.get("name"):
            creds[a["name"]] = obtain_auth_header(base_url, a)
    for c in target.get("acceptance_checks", []):
        k = c["kind"]
        hdrs = None
        actor = c.get("as_actor") or c.get("as")
        if actor is not None:
            ah, aerr = creds.get(actor, (None, f"actor {actor!r} not declared"))
            if ah is None:
                out.append({"kind": k, "route": c.get("route"), "ok": False,
                            "detail": f"auth required but {aerr}"})
                continue
            hdrs = ah
        elif c.get("auth"):
            ah, aerr = creds.get("", (None, "no auth flow declared"))
            if ah is None:
                out.append({"kind": k, "route": c.get("route"), "ok": False,
                            "detail": f"auth required but {aerr or 'no auth flow declared'}"})
                continue
            hdrs = ah
        if k == "route_status":
            out.append(check_route_status(base_url, c["route"], c.get("expected_status", 200), headers=hdrs))
        elif k == "selector_present":
            out.append(check_selector_present(base_url, c["route"], c["selector"]))
        elif k == "mobile_fit":
            out.append(check_mobile_fit(base_url, c["route"]))
        elif k == "api_json":
            out.append(check_api_json(base_url, c["route"], c.get("method", "GET"),
                                      c.get("body"), c.get("json_path"), c.get("json_equals"), headers=hdrs))
        elif k == "persistence_survives_restart":
            if restart is None:
                out.append({"kind": k, "ok": False,
                            "detail": "persistence check requires a bootable backend"})
            else:
                out.append(check_persistence_survives_restart(
                    base_url, c["route"], c.get("method", "POST"), c.get("body"),
                    c.get("json_path"), c.get("verify_route"), restart, headers=hdrs))
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
