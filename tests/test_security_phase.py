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
