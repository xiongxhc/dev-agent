"""SecurityVerifyPhase — orchestrates the deterministic probes + fail-safe triage + gating
ruleset against a brought-up preview's base_urls. Invoked at a live-base_urls call site
(build_system.reverify feeding M23; single-run after DeployPhase), NEVER appended to
build_pipeline_phases (those per-service sub-runs run deploy=False before bring-up, so
base_urls never exist there and the phase would no-op on every service)."""

from dataclasses import dataclass, field

from ..acceptance_runner import obtain_auth_header
from ..contract_utils import auth_flow_from_contract
from .findings import Finding
from .probes import run_probes
from .ruleset import intentional_open_pairs, partition
from .triage import triage


@dataclass
class SecurityVerifyResult:
    gating_steps: list = field(default_factory=list)   # rendered failing steps for M23
    findings: list = field(default_factory=list)       # all findings (gating + advisory)
    not_run: list = field(default_factory=list)        # probe classes reported not-run


class SecurityVerifyPhase:
    def __init__(self, design, *, triage_client=None, http=None, second_principal=True):
        self.design = design
        self.triage_client = triage_client
        self.http = http
        self.second_principal = second_principal

    def _principals(self, base_url, contract):
        """(a_headers, b_headers|None). A is the primary flow; B is a distinct registered user
        when registration is open (or a provisioning path exists). Returns (None, None) when no
        flow is derivable."""
        flow = auth_flow_from_contract(contract)
        if flow is None:
            return None, None
        a, _ = obtain_auth_header(base_url, flow)
        b = None
        if self.second_principal and flow.get("register_route"):
            flow_b = dict(flow)
            reg = dict(flow.get("register_body") or flow.get("login_body") or {})
            login = dict(flow.get("login_body") or {})
            for creds in (reg, login):
                for k in list(creds):
                    if isinstance(creds[k], str) and "runner" in creds[k]:
                        creds[k] = creds[k].replace("runner", "runner_b", 1)
            flow_b["register_body"], flow_b["login_body"] = reg, login
            b, _ = obtain_auth_header(base_url, flow_b)
        return a, b

    def verify(self, base_urls) -> SecurityVerifyResult:
        res = SecurityVerifyResult()
        if not base_urls:
            return res
        open_pairs = intentional_open_pairs(self.design)
        by_producer: dict = {}
        for c in getattr(self.design, "contracts", []):
            if getattr(c, "kind", None) == "openapi":
                by_producer.setdefault(c.producer, []).append(c)
        for sid, contracts in by_producer.items():
            base = base_urls.get(sid)
            if not base:
                continue
            for contract in contracts:
                a, b = self._principals(base, contract)
                auth = {"a": a, "b": b}
                findings = run_probes(base, sid, contract, auth, http=self.http)
                findings += triage(base, sid, contract, findings, client=self.triage_client)
                # dedup by (kind, route, method)
                seen, deduped = set(), []
                for f in findings:
                    key = (f.kind, f.route, f.method)
                    if key not in seen:
                        seen.add(key)
                        deduped.append(f)
                if b is None:
                    res.not_run.append("idor")
                    deduped.append(Finding(
                        kind="idor", service=sid, route="(class)", method="-",
                        severity="low", confidence="low",
                        evidence="IDOR/authz probe class NOT RUN: no second principal obtainable "
                                 "(registration closed and no declared provisioning path)",
                        remediation="declare a seed/admin-create path so cross-user checks run"))
                gating, _advisory = partition(
                    [f for f in deduped if not (f.kind == "idor" and f.route == "(class)")],
                    open_pairs)
                res.gating_steps.extend(g.as_failing_step() for g in gating)
                res.findings.extend(deduped)
        return res


def verify_callable(design, *, triage_client=None, http=None):
    """Factory for the M23 `security_verify` default (wired in by the CLI, Task 6): binds one
    design plus triage/http config and returns a (design, base_urls) -> SecurityVerifyResult
    callable matching build_system.reverify's call convention (`security_verify(design,
    base_urls)`). The design argument on the returned callable is accepted for that convention
    but ignored — the design captured at construction time is authoritative."""
    phase = SecurityVerifyPhase(design, triage_client=triage_client, http=http)

    def _verify(_design, base_urls) -> SecurityVerifyResult:
        return phase.verify(base_urls)

    return _verify
