from devagent.security.findings import Finding


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
