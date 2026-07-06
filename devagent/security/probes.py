"""Deterministic security probe library, keyed off the frozen OpenAPI contract. Each probe is
app-specific (derived from the contract paths + request/response schemas) and pure enough to
unit-test against a fixture: HTTP callers are injected. Probes MUTATE state (inject role=admin,
cross-user writes) — safe only against a disposable preview (the phase enforces that)."""

from .findings import Finding
from ..contract_utils import (_method_op, _paths, _protected, _request_schema, sample_body)

MASS_ASSIGN_FIELDS = ("role", "is_admin", "isAdmin", "isVerified", "admin")
_MUTATING = ("POST", "PUT", "PATCH", "DELETE")


class _Http:
    """Default adapter over acceptance_runner: returns dicts with a parsed _payload for
    reflection checks. Failures degrade to {'ok': False} — a dead route is a failed probe."""
    def api_json(self, base, route, method, body, json_path, json_equals, headers=None):
        # Not check_api_json: the probes need the raw parsed payload + status for reflection/IDOR
        # detection, which that verdict-only helper doesn't expose. Kept separate deliberately.
        import json
        import urllib.request
        url = base.rstrip("/") + route
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read().decode())
                return {"ok": 200 <= r.status < 300, "detail": f"status {r.status}",
                        "_payload": payload, "_status": r.status}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"request failed: {e}", "_payload": None,
                    "_status": None}
    def route_status(self, base, route, expected_status, headers=None):
        from ..acceptance_runner import check_route_status
        return check_route_status(base, route, expected_status, headers=headers)


def _reflects(payload, key, value) -> bool:
    """True if the injected (key,value) is echoed anywhere in the response payload — the
    mass-assignment signature (the server accepted a field it should have stripped)."""
    if isinstance(payload, dict):
        if str(payload.get(key)) == str(value):
            return True
        return any(_reflects(v, key, value) for v in payload.values())
    if isinstance(payload, list):
        return any(_reflects(v, key, value) for v in payload)
    return False


def probe_mass_assignment(base_url, service, contract, auth, *, http=None) -> list[Finding]:
    http = http or _Http()
    out: list[Finding] = []
    for route, methods in _paths(contract).items():
        if "{" in route:
            continue
        for verb in ("post", "put"):
            op = _method_op(methods, verb)
            if op is None:
                continue
            base_body = sample_body(_request_schema(op)) if _request_schema(op) else {}
            if not isinstance(base_body, dict):
                base_body = {}
            hdrs = (auth or {}).get("a")
            for field in MASS_ASSIGN_FIELDS:
                body = {**base_body, field: "admin" if field in ("role",) else True}
                r = http.api_json(base_url, route, verb.upper(), body, None, None, headers=hdrs)
                if r.get("_payload") is not None and _reflects(r["_payload"], field,
                                                               body[field]):
                    out.append(Finding(
                        kind="mass_assignment", service=service, route=route,
                        method=verb.upper(), severity="critical", confidence="high",
                        evidence=f"POST/PUT {route} with {{{field}: {body[field]!r}}} was "
                                 f"reflected in the response — privilege field accepted",
                        remediation=f"strip {field!r} from the request payload; set it "
                                    "server-side from the authenticated principal"))
                    break   # one privilege field per route is enough to gate
    return out


def probe_missing_authz(base_url, service, contract, auth, *, http=None) -> list[Finding]:
    http = http or _Http()
    out: list[Finding] = []
    for route, methods in _paths(contract).items():
        if "{" in route:
            continue
        get = _method_op(methods, "get")
        if get is not None and _protected(get):
            r = http.route_status(base_url, route, 200, headers=None)   # NO token
            if r.get("ok"):
                out.append(Finding(
                    kind="missing_authz", service=service, route=route, method="GET",
                    severity="high", confidence="high",
                    evidence=f"GET {route} is declared protected (security) but returned 2xx "
                             "with no credential",
                    remediation="enforce authentication middleware on this route"))
    return out


def probe_idor(base_url, service, contract, auth, *, http=None) -> list[Finding]:
    """Cross-user read/mutate. Requires principal B (auth['b']); when absent, returns [] and
    the phase reports the class not-run. Deterministic: create as A, access as B, expect
    403/404; a 2xx is the finding."""
    http = http or _Http()
    if not (auth or {}).get("b"):
        return []
    a_h, b_h = auth["a"], auth["b"]
    out: list[Finding] = []
    for route, methods in _paths(contract).items():
        post = _method_op(methods, "post")
        if "{" in route or post is None:
            continue
        body = sample_body(_request_schema(post)) if _request_schema(post) else {}
        created = http.api_json(base_url, route, "POST",
                                body if isinstance(body, dict) else None, None, None, headers=a_h)
        payload = created.get("_payload") or {}
        rid = payload.get("id") if isinstance(payload, dict) else None
        if rid is None:
            continue
        item_route = route.rstrip("/") + f"/{rid}"
        r = http.api_json(base_url, item_route, "GET", None, None, None, headers=b_h)
        if r.get("_status") is not None and 200 <= r["_status"] < 300:
            out.append(Finding(
                kind="idor", service=service, route=item_route, method="GET",
                severity="high", confidence="high",
                evidence=f"user B read user A's resource at {item_route} (2xx); "
                         "no ownership check",
                remediation="scope the query to the authenticated principal / return 403"))
    return out


def probe_weak_registration(base_url, service, contract, auth, *, http=None) -> list[Finding]:
    """An open (security-free) register/signup POST. Reported as a weak_registration finding;
    whether it GATES is the ruleset's call (advisory unless the design does not declare the
    route intentionally-open)."""
    out: list[Finding] = []
    for route, methods in _paths(contract).items():
        post = _method_op(methods, "post")
        if post is None or ("register" not in route and "signup" not in route):
            continue
        if not _protected(post):
            out.append(Finding(
                kind="weak_registration", service=service, route=route, method="POST",
                severity="medium", confidence="medium",
                evidence=f"{route} accepts unauthenticated POST (open registration)",
                remediation="if unintended, gate signup (invite/admin-create); otherwise "
                            "declare the route intentionally-open in the design"))
    return out


def probe_verb_tampering(base_url, service, contract, auth, *, http=None) -> list[Finding]:
    http = http or _Http()
    out: list[Finding] = []
    for route, methods in _paths(contract).items():
        if "{" in route:
            continue
        declared = {str(m).upper() for m in methods if isinstance(methods.get(m), dict)}
        for verb in _MUTATING:
            if verb in declared:
                continue
            r = http.api_json(base_url, route, verb, {}, None, None,
                              headers=(auth or {}).get("a"))
            if r.get("_status") is not None and 200 <= r["_status"] < 300:
                out.append(Finding(
                    kind="verb_tampering", service=service, route=route, method=verb,
                    severity="medium", confidence="medium",
                    evidence=f"{verb} {route} returned 2xx though the contract declares only "
                             f"{sorted(declared)}",
                    remediation="reject undeclared methods with 405"))
                break
    return out


_PROBES = (probe_mass_assignment, probe_missing_authz, probe_idor,
           probe_weak_registration, probe_verb_tampering)


def run_probes(base_url, service, contract, auth, *, http=None) -> list[Finding]:
    """Run every deterministic probe against one service's base_url + contract. Returns the
    flat findings list (may be empty). Never raises — a probe crash yields no finding for
    that class (fail-open on the probe, fail-closed on the gate is the ruleset's job)."""
    out: list[Finding] = []
    for probe in _PROBES:
        try:
            out.extend(probe(base_url, service, contract, auth, http=http))
        except Exception:  # noqa: BLE001 — one probe's crash must not sink the others
            continue
    return out
