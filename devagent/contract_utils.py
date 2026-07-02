"""M16 — pure contract helpers. `contracts_for_node` selects the contracts a service consumes
(for read-only injection into its build prompt); `openapi_to_checks` turns an openapi Contract's
paths into acceptance-check dicts a ContractConformanceGate can run against the built service.
Pure and registry-free — no Docker, no tokens."""

from .schema import Contract, ServiceNode, SystemDesign


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
