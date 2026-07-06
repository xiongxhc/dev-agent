"""M16 — pure contract helpers. `contracts_for_node` selects the contracts a service consumes
(for read-only injection into its build prompt); `openapi_to_checks` turns an openapi Contract's
paths into acceptance-check dicts a ContractConformanceGate can run against the built service.

One-flow (2026-07-03): `derive_checks` / `derive_persistence_check` / `derive_integration_checks`
generate the FULL check set mechanically from a contract's schemas — the same checks grade a
service in-container (acceptance) and against the brought-up system (integration), so there is
exactly one spec authority. (Live-run finding: LLM-written checks in the sub-run's scope
contradicted the frozen contract — object vs array root for GET /polls — making both
unsatisfiable; the builder shipped a hybrid response that broke the contract-correct frontend.)

Pure and registry-free — no Docker, no tokens."""

import re

from .schema import Contract, IntegrationCheck, ServiceNode, SystemDesign


def contracts_for_node(node: ServiceNode, design: SystemDesign) -> list[Contract]:
    """The contracts `node` consumes — the interfaces its build must conform to. NOTE: when
    wiring into the build prompt, inject each contract's `.spec` (a JSON-serializable dict),
    NOT the Contract object — build_prompt json.dumps the injected value."""
    return [c for c in design.contracts if c.id in node.consumes]


def openapi_to_checks(contract: Contract) -> list[dict]:
    """Convert an openapi Contract's paths into conformance checks. M16 emits ONLY safe,
    unambiguous probes: a route_status (expect 200) for each GET on a NON-templated path.
    Non-GET methods are skipped (a read-only verification must never issue mutating
    POST/PUT/DELETE against the live service); templated paths like /x/{id} are skipped (no
    real param value to substitute); non-method keys (parameters/summary/$ref) never match
    "get" so are ignored; a non-dict path item is skipped. Returns [] for non-openapi
    contracts. (Auth-protected and mutating endpoints are out of conformance scope here — they
    are covered by each service's own acceptance checks.)"""
    if contract.kind != "openapi":
        return []
    checks: list[dict] = []
    for path, methods in contract.spec.get("paths", {}).items():
        if not isinstance(methods, dict) or "{" in path:
            continue
        for method in methods:
            if str(method).lower() == "get":
                checks.append({"kind": "route_status", "route": path, "expected_status": 200})
    return checks


def _resolve_refs(node, spec, depth=0):
    """Inline local '#/a/b/c' $ref pointers so every schema consumer sees concrete shapes —
    architects legitimately emit components/schemas + $ref (live run, 2026-07-06: unresolved
    refs made auth underivable and every protected check was silently stripped). Depth-capped
    against self-referential schemas; a dangling ref resolves to {} (derives nothing, same as
    an absent schema)."""
    if depth > 10:
        return {}
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = spec
            for part in ref[2:].split("/"):
                target = target.get(part) if isinstance(target, dict) else None
            return _resolve_refs(target, spec, depth + 1) if target is not None else {}
        return {k: _resolve_refs(v, spec, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, spec, depth + 1) for v in node]
    return node


def _paths(contract: Contract) -> dict:
    """The contract's paths with every local $ref inlined."""
    return _resolve_refs(contract.spec.get("paths") or {}, contract.spec)


def _first_2xx_schema(op) -> dict | None:
    """The JSON schema of an operation's first (lowest) 2xx application/json response."""
    if not isinstance(op, dict):
        return None
    responses = {str(k): v for k, v in (op.get("responses") or {}).items()}
    for status in sorted(k for k in responses if k.startswith("2")):
        content = (responses[status] or {}).get("content") or {}
        schema = (content.get("application/json") or {}).get("schema")
        if isinstance(schema, dict):
            return schema
    return None


def _first_prop(schema) -> str | None:
    """The property name to assert presence of: 'id' if declared, else the first declared
    property. An array root delegates to its `items` schema."""
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "array":
        return _first_prop(schema.get("items"))
    props = schema.get("properties") or {}
    if not props:
        return None
    return "id" if "id" in props else next(iter(props))


def _request_schema(op) -> dict | None:
    if not isinstance(op, dict):
        return None
    content = (op.get("requestBody") or {}).get("content") or {}
    schema = (content.get("application/json") or {}).get("schema")
    return schema if isinstance(schema, dict) else None


def sample_body(schema, name: str = ""):
    """Deterministic sample value satisfying *schema*, for synthesized POST bodies.
    required-only objects (all properties when nothing is required), 2-element arrays
    (collections like poll options commonly require >= 2), name-aware strings so
    email/password validators don't reject the sample. Strings carry no spaces — a
    'sample username' 400s against alphanumeric-username validators (live run, 2026-07-06)."""
    if not isinstance(schema, dict):
        return "sample"
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = [k for k in (schema.get("required") or []) if k in props]
        return {k: sample_body(props[k], k) for k in (required or list(props))}
    if t == "array":
        item = schema.get("items") or {}
        return [sample_body(item, name), sample_body(item, name)]
    if t in ("integer", "number"):
        return 1
    if t == "boolean":
        return True
    low = name.lower()
    if "email" in low:
        return "sample@example.com"
    if "password" in low:
        return "Sample-Passw0rd-1!"
    return f"sample_{name}".rstrip("_") if name else "sample"


def _method_op(methods: dict, wanted: str):
    return next((v for k, v in methods.items()
                 if str(k).lower() == wanted and isinstance(v, dict)), None)


def _protected(op) -> bool:
    """Truthy `security` on the operation = needs a credential (the architect prompt requires
    the marker on every protected op)."""
    return bool(isinstance(op, dict) and op.get("security"))


def _role_gated(op) -> bool:
    """`x-required-role` = only a privileged actor gets 200. The derived default flow is a
    REGULAR member (prompt rule), so the only mechanical assertion is the negative one."""
    return bool(isinstance(op, dict) and op.get("x-required-role"))


def _runnerize(body):
    """The auth FLOW's creds, distinct from the derived register CHECK's sample user (same
    schema, 'runner' prefix) so the two never collide on a uniqueness constraint."""
    if isinstance(body, dict):
        return {k: _runnerize(v) for k, v in body.items()}
    if isinstance(body, str) and body.startswith("sample"):
        return "runner" + body[len("sample"):]
    return body


def auth_flow_from_contract(contract: Contract) -> dict | None:
    """Synthesize the verify harness's AuthFlow from the contract's own auth endpoints: the
    first plain POST path containing 'login' (plus 'register' if present). token_json_path is
    the login 2xx schema's 'token' property (prompt rule) or its first property. Returns None
    when the contract declares no login — protected ops are then unverifiable mechanically.
    A login op WITHOUT a request schema still derives: default username/password creds — the
    build prompt's auth-contract line makes the builder accept exactly these fields."""
    if contract.kind != "openapi":
        return None
    paths = _paths(contract)
    def _find(word):
        return next((p for p, m in paths.items()
                     if word in p and "{" not in p and isinstance(m, dict)
                     and _method_op(m, "post") is not None), None)
    login = _find("login")
    if login is None:
        return None
    login_op = _method_op(paths[login], "post")
    creds = _runnerize(sample_body(_request_schema(login_op)) or {})
    if not isinstance(creds, dict) or not creds:
        creds = {"username": "runner_user", "password": "Runner-Passw0rd-1!"}
    schema = _first_2xx_schema(login_op) or {}
    props = schema.get("properties") or {}
    token_path = "token" if "token" in props else (next(iter(props), None) or "token")
    flow = {"login_route": login, "login_body": creds,
            "token_json_path": token_path, "mode": "bearer"}
    register = _find("register")
    if register is not None:
        reg = _runnerize(sample_body(_request_schema(_method_op(paths[register], "post"))) or {})
        flow["register_route"] = register
        flow["register_body"] = {**reg, **creds} if isinstance(reg, dict) else creds
    return flow


def derive_checks(contract: Contract) -> list[dict]:
    """The full mechanical check set for an openapi contract, in run order:

      1. route_status for every plain (non-templated) GET: 200 for public ops; for protected
         ops (`security`) an unauthenticated 401 probe PLUS an authenticated 200 probe; for
         role-gated ops (`x-required-role`) ONLY the member-gets-403 probe — the default
         derived flow is a regular member, so a privileged 200 is not mechanically provable.
      2. api_json POST for every plain POST, body synthesized from the request schema,
         asserting the first declared response property is present (auth per the op).
      3. api_json GET re-reading every path a step-2 POST created, asserting the ROOT SHAPE
         the contract declares (array root -> "0.<prop>", object root -> "<prop>").
      4. Templated paths under a created collection, every param substituted with 1 (the
         first row on a fresh datastore — the same rule the architect prompt states).

    Ordering matters: mutations precede the reads that assert on them. Returns [] for
    non-openapi contracts. NOTE: callers must drop auth=True checks when no auth flow is
    derivable (auth_flow_from_contract is None) — an auth'd check without a flow fails
    ArtifactSpec validation."""
    if contract.kind != "openapi":
        return []
    paths = _paths(contract)
    plain = {p: m for p, m in paths.items() if isinstance(m, dict) and "{" not in p}
    templated = {p: m for p, m in paths.items() if isinstance(m, dict) and "{" in p}
    checks: list[dict] = []

    def _with_auth(check: dict, op) -> dict:
        if _protected(op):
            check["auth"] = True
        return check

    for path, methods in plain.items():
        get = _method_op(methods, "get")
        if get is None:
            continue
        if _role_gated(get):
            checks.append({"kind": "route_status", "route": path, "expected_status": 403,
                           "auth": True})
        elif _protected(get):
            checks.append({"kind": "route_status", "route": path, "expected_status": 401})
            checks.append({"kind": "route_status", "route": path, "expected_status": 200,
                           "auth": True})
        else:
            checks.append({"kind": "route_status", "route": path, "expected_status": 200})

    created: list[str] = []
    for path, methods in plain.items():
        post = _method_op(methods, "post")
        if post is None or _role_gated(post):
            continue
        body = sample_body(_request_schema(post)) if _request_schema(post) else None
        checks.append(_with_auth({"kind": "api_json", "route": path, "method": "POST",
                                  "body": body if isinstance(body, dict) else None,
                                  "json_path": _first_prop(_first_2xx_schema(post))}, post))
        created.append(path)

    for path in created:
        get = _method_op(plain[path], "get")
        schema = _first_2xx_schema(get)
        prop = _first_prop(schema)
        if get is None or prop is None or _role_gated(get):
            continue
        jp = f"0.{prop}" if schema.get("type") == "array" else prop
        checks.append(_with_auth({"kind": "api_json", "route": path, "method": "GET",
                                  "json_path": jp}, get))

    for path, methods in templated.items():
        if not any(path.startswith(c + "/") for c in created):
            continue
        route = re.sub(r"\{[^/}]+\}", "1", path)
        for wanted in ("get", "post"):
            op = _method_op(methods, wanted)
            if op is None or _role_gated(op):
                continue
            body = sample_body(_request_schema(op)) if _request_schema(op) else None
            checks.append(_with_auth({"kind": "api_json", "route": route,
                                      "method": wanted.upper(),
                                      "body": body if isinstance(body, dict) else None,
                                      "json_path": _first_prop(_first_2xx_schema(op))}, op))
    return checks


def derive_persistence_check(contract: Contract) -> dict | None:
    """A durability check from the first plain path with both POST and GET: create a row,
    restart the app (the datastore stays up), read it back. Only meaningful when the service
    HAS a datastore — the caller gates on that."""
    if contract.kind != "openapi":
        return None
    for path, methods in _paths(contract).items():
        if not isinstance(methods, dict) or "{" in path:
            continue
        post, get = _method_op(methods, "post"), _method_op(methods, "get")
        if post is None or get is None or _role_gated(post) or _role_gated(get):
            continue
        check = {"kind": "persistence_survives_restart", "route": path, "method": "POST",
                 "body": None, "json_path": _first_prop(_first_2xx_schema(post)) or "id",
                 "verify_route": path}
        body = sample_body(_request_schema(post)) if _request_schema(post) else None
        if isinstance(body, dict):
            check["body"] = body
        if _protected(post) or _protected(get):
            check["auth"] = True
        return check
    return None


def derive_integration_checks(design: SystemDesign) -> list[IntegrationCheck]:
    """ONE integration check set, derived from the design's contracts: every producer is
    re-graded against the brought-up system with the same shape assertions it passed
    in-container, plus a root probe per frontend node. This replaces the architect's
    free-written integration_checks as the gate's authority — two LLM check sets over one
    contract contradicted each other in live runs; a derived set cannot.

    Protected checks (auth=True) are dropped: IntegrationRunner has no auth-flow support, and
    every protected op was already fully graded in-container (where the acceptance runner DOES
    log in). Integration proves the system is up and wired, not auth depth."""
    by_producer: dict[str, list[dict]] = {}
    for c in design.contracts:
        if c.kind == "openapi":
            by_producer.setdefault(c.producer, []).extend(
                k for k in derive_checks(c)
                if not k.get("auth") and k.get("expected_status", 200) < 400)
    out: list[IntegrationCheck] = []
    for node in design.services:
        for chk in by_producer.get(node.id, []):
            out.append(IntegrationCheck(
                service=node.id, route=chk["route"], method=chk.get("method", "GET"),
                body=chk.get("body"), json_path=chk.get("json_path"),
                expected_status=chk.get("expected_status", 200)))
        if node.kind == "frontend":
            out.append(IntegrationCheck(service=node.id, route="/", expected_status=200))
    return out
