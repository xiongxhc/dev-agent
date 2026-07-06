from devagent.security.findings import Finding
from devagent.schema import Contract
from devagent.security.probes import run_probes, probe_mass_assignment


def _register_contract():
    return Contract(id="c", kind="openapi", producer="api", spec={"paths": {
        "/auth/register": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {"type": "object",
                "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
                "required": ["username", "password"]}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "object",
                "properties": {"token": {"type": "string"}, "role": {"type": "string"}}}}}}}}}}})


class _FakeHttp:
    """Records requests; returns scripted JSON. `reflect` echoes injected fields back (the vuln)."""
    def __init__(self, reflect=False, status=200):
        self.reflect, self.status, self.calls = reflect, status, []
    def api_json(self, base, route, method, body, json_path, json_equals, headers=None):
        self.calls.append((route, method, body, headers))
        payload = {"token": "t"}
        if self.reflect and isinstance(body, dict):
            payload.update({k: v for k, v in body.items()})   # echoes role=admin -> vuln
        return {"ok": self.status == 200, "detail": "", "_payload": payload}
    def route_status(self, base, route, expected_status, headers=None):
        self.calls.append((route, "GET", None, headers))
        return {"ok": self.status == expected_status, "detail": f"status {self.status}"}


def test_mass_assignment_trips_on_reflected_role():
    http = _FakeHttp(reflect=True)
    findings = probe_mass_assignment("http://api", "api", _register_contract(),
                                     auth={"a": None, "b": None}, http=http)
    kinds = {f.kind for f in findings}
    assert "mass_assignment" in kinds
    hit = next(f for f in findings if f.kind == "mass_assignment")
    assert hit.route == "/auth/register" and hit.severity == "critical"


def test_mass_assignment_silent_when_not_reflected():
    http = _FakeHttp(reflect=False)
    findings = probe_mass_assignment("http://api", "api", _register_contract(),
                                     auth={"a": None, "b": None}, http=http)
    assert [f for f in findings if f.kind == "mass_assignment"] == []


def test_run_probes_returns_findings_list():
    http = _FakeHttp(reflect=True)
    findings = run_probes("http://api", "api", _register_contract(),
                          auth={"a": None, "b": None}, http=http)
    assert isinstance(findings, list) and all(hasattr(f, "kind") for f in findings)


def test_finding_renders_as_failing_step():
    f = Finding(kind="mass_assignment", service="api", route="/auth/register", method="POST",
                severity="critical", confidence="high",
                evidence="POST body {role: admin} reflected in token",
                remediation="strip role from the registration payload; assign server-side")
    step = f.as_failing_step()
    assert step == {"service": "api", "route": "/auth/register", "ok": False,
                    "detail": "POST body {role: admin} reflected in token — "
                              "strip role from the registration payload; assign server-side"}


def test_finding_confidence_and_kind_are_constrained():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Finding(kind="not_a_real_kind", service="api", route="/", method="GET",
                severity="low", confidence="high", evidence="x", remediation="y")
    with pytest.raises(ValidationError):
        Finding(kind="idor", service="api", route="/", method="GET",
                severity="low", confidence="maybe", evidence="x", remediation="y")
