from devagent.schema import Contract
from devagent.security.triage import triage


def _contract():
    return Contract(id="c", kind="openapi", producer="api",
                    spec={"paths": {"/auth/register": {"post": {}}}})


def test_triage_api_down_returns_empty_not_raise():
    class _BoomClient:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("API down")
    result = triage("http://api", "api", _contract(), [], client=_BoomClient())
    assert result == []      # fail-safe: deterministic findings already gate


def test_triage_returns_only_allowlisted_finding_kinds(monkeypatch):
    from devagent.security import triage as triage_mod
    from devagent.security.findings import Finding
    # Stub generate_structured to return a triage wrapper with one valid finding.
    def fake_gs(prompt, schema, **kw):
        obj = schema.model_validate({"findings": [{
            "kind": "mass_assignment", "service": "api", "route": "/auth/register",
            "method": "POST", "severity": "critical", "confidence": "high",
            "evidence": "e", "remediation": "r"}]})
        return obj, {"tokens_in": 1, "tokens_out": 1}
    monkeypatch.setattr(triage_mod, "generate_structured", fake_gs)
    result = triage("http://api", "api", _contract(), [], client=object())
    assert len(result) == 1 and isinstance(result[0], Finding)
    assert result[0].kind == "mass_assignment"
