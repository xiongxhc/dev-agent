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
    email/password validators don't reject the sample."""
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
    return f"sample {name}".strip() or "sample"


def _method_op(methods: dict, wanted: str):
    return next((v for k, v in methods.items()
                 if str(k).lower() == wanted and isinstance(v, dict)), None)


def derive_checks(contract: Contract) -> list[dict]:
    """The full mechanical check set for an openapi contract, in run order:

      1. route_status 200 for every plain (non-templated) GET — safe on an empty datastore.
      2. api_json POST for every plain POST, body synthesized from the request schema,
         asserting the first declared response property is present.
      3. api_json GET re-reading every path a step-2 POST created, asserting the ROOT SHAPE
         the contract declares (array root -> "0.<prop>", object root -> "<prop>").
      4. Templated paths under a created collection, every param substituted with 1 (the
         first row on a fresh datastore — the same rule the architect prompt states).

    Ordering matters: mutations precede the reads that assert on them. Returns [] for
    non-openapi contracts."""
    if contract.kind != "openapi":
        return []
    paths = contract.spec.get("paths") or {}
    plain = {p: m for p, m in paths.items() if isinstance(m, dict) and "{" not in p}
    templated = {p: m for p, m in paths.items() if isinstance(m, dict) and "{" in p}
    checks: list[dict] = []

    for path, methods in plain.items():
        if _method_op(methods, "get") is not None:
            checks.append({"kind": "route_status", "route": path, "expected_status": 200})

    created: list[str] = []
    for path, methods in plain.items():
        post = _method_op(methods, "post")
        if post is None:
            continue
        body = sample_body(_request_schema(post)) if _request_schema(post) else None
        checks.append({"kind": "api_json", "route": path, "method": "POST",
                       "body": body if isinstance(body, dict) else None,
                       "json_path": _first_prop(_first_2xx_schema(post))})
        created.append(path)

    for path in created:
        get = _method_op(plain[path], "get")
        schema = _first_2xx_schema(get)
        prop = _first_prop(schema)
        if get is None or prop is None:
            continue
        jp = f"0.{prop}" if schema.get("type") == "array" else prop
        checks.append({"kind": "api_json", "route": path, "method": "GET", "json_path": jp})

    for path, methods in templated.items():
        if not any(path.startswith(c + "/") for c in created):
            continue
        route = re.sub(r"\{[^/}]+\}", "1", path)
        for wanted in ("get", "post"):
            op = _method_op(methods, wanted)
            if op is None:
                continue
            body = sample_body(_request_schema(op)) if _request_schema(op) else None
            checks.append({"kind": "api_json", "route": route, "method": wanted.upper(),
                           "body": body if isinstance(body, dict) else None,
                           "json_path": _first_prop(_first_2xx_schema(op))})
    return checks


def derive_persistence_check(contract: Contract) -> dict | None:
    """A durability check from the first plain path with both POST and GET: create a row,
    restart the app (the datastore stays up), read it back. Only meaningful when the service
    HAS a datastore — the caller gates on that."""
    if contract.kind != "openapi":
        return None
    for path, methods in (contract.spec.get("paths") or {}).items():
        if not isinstance(methods, dict) or "{" in path:
            continue
        post, get = _method_op(methods, "post"), _method_op(methods, "get")
        if post is None or get is None:
            continue
        body = sample_body(_request_schema(post)) if _request_schema(post) else None
        return {"kind": "persistence_survives_restart", "route": path, "method": "POST",
                "body": body if isinstance(body, dict) else None,
                "json_path": _first_prop(_first_2xx_schema(post)) or "id",
                "verify_route": path}
    return None


def derive_integration_checks(design: SystemDesign) -> list[IntegrationCheck]:
    """ONE integration check set, derived from the design's contracts: every producer is
    re-graded against the brought-up system with the same shape assertions it passed
    in-container, plus a root probe per frontend node. This replaces the architect's
    free-written integration_checks as the gate's authority — two LLM check sets over one
    contract contradicted each other in live runs; a derived set cannot."""
    by_producer: dict[str, list[dict]] = {}
    for c in design.contracts:
        if c.kind == "openapi":
            by_producer.setdefault(c.producer, []).extend(derive_checks(c))
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
