from types import SimpleNamespace

from devagent.schema import Contract, ServiceNode, SystemDesign
from devagent.security.phase import SecurityVerifyPhase, SecurityVerifyResult


def _vuln_design():
    return SystemDesign(title="t", services=[
        ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                    prd_slice="x", provides=["c"])],
        contracts=[Contract(id="c", kind="openapi", producer="api", spec={"paths": {
            "/auth/register": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"username": {"type": "string"}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"token": {"type": "string"}}}}}}}
            }}
        }})])


class _ReflectHttp:
    def api_json(self, base, route, method, body, json_path, json_equals, headers=None):
        payload = {"token": "t"}
        if isinstance(body, dict):
            payload.update(body)                 # reflects role=admin -> mass-assignment vuln
        return {"ok": True, "detail": "", "_payload": payload, "_status": 200}
    def route_status(self, base, route, expected_status, headers=None):
        return {"ok": False, "detail": "401"}


class _SafeHttp:
    # A correct app: extra fields rejected (no reflection), protected routes answer 401.
    def api_json(self, base, route, method, body, json_path, json_equals, headers=None):
        return {"ok": False, "detail": "", "_payload": {"error": "Unexpected field"},
                "_status": 400}
    def route_status(self, base, route, expected_status, headers=None):
        return {"ok": False, "detail": "401"}


def test_triage_speculation_on_safe_app_does_not_gate(monkeypatch):
    # Live regression (2026-07-10, expense-app run): every deterministic probe passed against a
    # correct api, but the triage LLM returned a high-confidence mass_assignment finding by
    # READING the frozen OpenAPI contract. That speculation must surface as advisory, never as a
    # gating step — otherwise the M23 repair loop re-fires it on the unchanged contract forever
    # and a flawless build ends in integration_failed.
    from devagent.security import phase as phase_mod
    from devagent.security.findings import Finding
    def fake_triage(base, service, contract, found, *, client=None):
        return [Finding(kind="mass_assignment", service=service, route="/auth/register",
                        method="POST", severity="critical", confidence="high",
                        evidence="the spec likely accepts extra fields", remediation="r",
                        source="triage")]
    monkeypatch.setattr(phase_mod, "triage", fake_triage)
    phase = SecurityVerifyPhase(_vuln_design(), http=_SafeHttp(),
                                triage_client=object(), second_principal=False)
    res = phase.verify({"api": "http://api"})
    assert res.gating_steps == []                                   # no gate from speculation
    assert any(f.kind == "mass_assignment" for f in res.findings)   # still surfaced as advisory


def test_phase_gates_on_mass_assignment_and_renders_failing_step():
    phase = SecurityVerifyPhase(_vuln_design(), http=_ReflectHttp(),
                                triage_client=None, second_principal=False)
    res = phase.verify({"api": "http://api"})
    assert isinstance(res, SecurityVerifyResult)
    assert any(s["route"] == "/auth/register" and s["ok"] is False for s in res.gating_steps)
    assert any(f.kind == "mass_assignment" for f in res.findings)


def test_phase_reports_idor_not_run_without_second_principal():
    phase = SecurityVerifyPhase(_vuln_design(), http=_ReflectHttp(),
                                triage_client=None, second_principal=False)
    res = phase.verify({"api": "http://api"})
    assert "idor" in res.not_run          # explicit coverage gap, not a silent pass


def test_phase_noop_without_base_urls():
    phase = SecurityVerifyPhase(_vuln_design(), http=_ReflectHttp(), triage_client=None)
    res = phase.verify({})                 # nothing brought up
    assert res.gating_steps == [] and res.findings == []


def test_single_run_shim_no_contracts_no_ops():
    # The single-run call site builds a shim design with no openapi contracts (single runs hold
    # no probeable contract today). The phase must no-op: no probes, no gating steps, no findings.
    res = SecurityVerifyPhase(SimpleNamespace(contracts=[], services=[])).verify({"api": "http://api"})
    assert isinstance(res, SecurityVerifyResult)
    assert res.gating_steps == [] and res.findings == []
