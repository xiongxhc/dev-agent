"""The deterministic gating policy — the ONLY thing that can fail a run. Conservative by
construction: only unambiguous, high-confidence classes gate, and the contract escape hatch
suppresses a gate scoped to a (route, probe-class) pair, NEVER a whole endpoint. mass_assignment
is never escape-hatchable: 'signup is open' and 'signup may set its own role' are different
properties, and a public /auth/register that accepts role=admin is the exact July-3 bug."""

GATING_KINDS = frozenset({"mass_assignment", "missing_authz", "idor"})
# Classes a design MAY intentionally open (per (route, kind)); mass_assignment/idor never are.
_ESCAPE_HATCHABLE = frozenset({"missing_authz", "weak_registration"})


def gates(finding, open_pairs) -> bool:
    """True iff this finding fails the run. A gating-kind finding gates unless its
    (route, kind) is declared intentionally-open AND the kind is escape-hatchable."""
    if getattr(finding, "source", "probe") != "probe":
        return False    # only deterministic probes gate; triage classifies/expands (its prompt
                        # says so) — gating on spec speculation makes M23 repair unwinnable.
    if finding.kind not in GATING_KINDS:
        return False
    if (finding.kind in _ESCAPE_HATCHABLE
            and (finding.route, finding.kind) in open_pairs):
        return False
    return True


def partition(findings, open_pairs):
    """Split findings into (gating, advisory) by the policy above."""
    gating, advisory = [], []
    for f in findings:
        (gating if gates(f, open_pairs) else advisory).append(f)
    return gating, advisory


def intentional_open_pairs(design) -> set:
    """(route, kind) pairs a design declares intentionally-open. Reads `x-intentionally-open`
    on a contract op (a list of probe-class names, e.g. ['missing_authz']) — a per-(route,class)
    marker, never an endpoint-wide flag. Absent ⇒ empty set. Only openapi contracts are read."""
    pairs: set = set()
    for c in getattr(design, "contracts", []):
        if getattr(c, "kind", None) != "openapi":
            continue
        for route, methods in (c.spec.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for op in methods.values():
                if not isinstance(op, dict):
                    continue
                for kind in (op.get("x-intentionally-open") or []):
                    if kind in _ESCAPE_HATCHABLE:
                        pairs.add((route, kind))
    return pairs
