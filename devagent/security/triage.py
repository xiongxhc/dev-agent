"""The LLM seam. Fail-safe by construction: triage EXPANDS app-specific probes and CLASSIFIES
ambiguous responses, but it can only emit allowlisted Finding kinds (schema-enforced), it never
decides which findings gate (that is ruleset.py), and if the API is unreachable it returns []
so the deterministic probes still stand and still gate."""

import json

from pydantic import BaseModel

from ..llm import generate_structured
from .findings import Finding

_PROMPT = """\
You are a security reviewer inspecting a running preview of ONE service for defects that
functional acceptance checks cannot catch (mass-assignment, missing authorization, cross-user
IDOR, weak/open registration, method tampering).

Given the service's OpenAPI contract and the deterministic probe findings already collected,
return additional or refined findings ONLY. Each finding MUST use one of the allowed kinds and
name a real route from the contract. Do not restate a deterministic finding unless you are
downgrading a false positive to a lower confidence. You classify and expand; you do NOT decide
what blocks the build.

SERVICE: {service}
CONTRACT PATHS (JSON): {paths}
DETERMINISTIC FINDINGS (JSON): {found}
"""


class TriageFindings(BaseModel):
    findings: list[Finding] = []


def triage(base_url, service, contract, deterministic_findings, *, client=None) -> list[Finding]:
    """Return triage-produced Findings (allowlisted kinds only). Fail-safe: [] on any error."""
    try:
        paths = json.dumps((contract.spec or {}).get("paths", {}))[:6000]
        found = json.dumps([f.model_dump() for f in deterministic_findings])[:4000]
        prompt = _PROMPT.format(service=service, paths=paths, found=found)
        obj, _usage = generate_structured(prompt, TriageFindings, client=client)
        findings = list(obj.findings)
        for f in findings:
            f.source = "triage"    # provenance is ours to assign, not the LLM's — it never gates
        return findings
    except Exception:  # noqa: BLE001 — API down / malformed emit: deterministic findings stand
        return []
