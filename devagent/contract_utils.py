"""M16 — pure contract helpers. `contracts_for_node` selects the contracts a service consumes
(for read-only injection into its build prompt); `openapi_to_checks` turns an openapi Contract's
paths into acceptance-check dicts a ContractConformanceGate can run against the built service.
Pure and registry-free — no Docker, no tokens."""

from .schema import Contract, ServiceNode, SystemDesign


def contracts_for_node(node: ServiceNode, design: SystemDesign) -> list[Contract]:
    """The contracts `node` consumes — the interfaces its build must conform to."""
    return [c for c in design.contracts if c.id in node.consumes]


def openapi_to_checks(contract: Contract) -> list[dict]:
    """Convert an openapi Contract's paths into acceptance-check dicts (route_status for GET,
    api_json for other methods). Returns [] for non-openapi contracts (deferred kinds)."""
    if contract.kind != "openapi":
        return []
    checks: list[dict] = []
    for path, methods in contract.spec.get("paths", {}).items():
        for method in methods:
            m = method.upper()
            if m == "GET":
                checks.append({"kind": "route_status", "route": path, "expected_status": 200})
            else:
                checks.append({"kind": "api_json", "route": path, "method": m})
    return checks
