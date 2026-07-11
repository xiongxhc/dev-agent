"""The Finding schema — one security defect a probe (or triage) reports. `service` is known
at probe time (each probe targets exactly one base_urls entry) and is what M23's attribution
maps to a node; a GATING finding renders as one failing step in the exact shape
M23's implicated_nodes consumes."""

from typing import Literal

from pydantic import BaseModel, Field

# The allowlist. Triage may only emit these kinds (schema-enforced) — an LLM cannot invent a
# gating class. Matches the deterministic probe library in probes.py.
FindingKind = Literal[
    "mass_assignment",       # POST/PUT body injects role/is_admin/isVerified and it sticks
    "missing_authz",         # declared-protected route answers 2xx with no token
    "idor",                  # user A reads/mutates user B's resource -> 2xx
    "weak_registration",     # open signup where the contract implies it should be gated
    "verb_tampering",        # a method the route should reject is honored
]

Severity = Literal["low", "medium", "high", "critical"]
Confidence = Literal["low", "medium", "high"]


class Finding(BaseModel):
    kind: FindingKind
    service: str = Field(..., min_length=1)     # a SystemDesign service id / single-run target name
    route: str = Field(..., min_length=1)
    method: str = "GET"
    severity: Severity = "medium"
    confidence: Confidence = "medium"
    evidence: str = Field(..., min_length=1)    # what was sent and what came back
    remediation: str = Field(..., min_length=1)
    # Provenance. Only "probe" findings — a deterministic probe that OBSERVED the defect on the
    # live preview — may gate. Triage speculates about the frozen contract; its verdicts are a
    # function of the spec, not the running code, so gating on them makes the M23 repair loop
    # unwinnable (the same speculation re-fires on every re-verify). triage() stamps "triage".
    source: Literal["probe", "triage"] = "probe"

    def as_failing_step(self) -> dict:
        """Render for M23's failing-step interface: {service, route, ok, detail}."""
        return {"service": self.service, "route": self.route, "ok": False,
                "detail": f"{self.evidence} — {self.remediation}"}
