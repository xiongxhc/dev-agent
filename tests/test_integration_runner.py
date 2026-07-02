from devagent.schema import IntegrationCheck
from devagent.integration import IntegrationRunner, IntegrationReport


def test_all_steps_pass():
    checks = [IntegrationCheck(service="api", route="/api/todos"),
              IntegrationCheck(service="web", route="/")]
    rep = IntegrationRunner(check_fn=lambda *a, **k: {"ok": True}).run(
        checks, {"api": "http://api", "web": "http://web"})
    assert isinstance(rep, IntegrationReport) and rep.ok
    assert [s["service"] for s in rep.steps] == ["api", "web"]


def test_a_failing_step_fails_the_report():
    checks = [IntegrationCheck(service="api", route="/api/todos")]
    rep = IntegrationRunner(check_fn=lambda *a, **k: {"ok": False, "detail": "500"}).run(
        checks, {"api": "http://api"})
    assert not rep.ok and rep.steps[0]["detail"] == "500"


def test_missing_base_url_is_a_failed_step():
    checks = [IntegrationCheck(service="api", route="/x")]
    rep = IntegrationRunner(check_fn=lambda *a, **k: {"ok": True}).run(checks, {})
    assert not rep.ok and not rep.steps[0]["ok"]


def test_empty_checks_is_not_ok():
    # nothing to prove -> not a pass (an integration verify with zero flows proved nothing)
    rep = IntegrationRunner(check_fn=lambda *a, **k: {"ok": True}).run([], {"api": "http://api"})
    assert not rep.ok


def test_dispatches_to_the_right_service_base_url():
    seen = []
    def fake(base_url, route, method, body, json_path, json_equals):
        seen.append((base_url, route)); return {"ok": True}
    checks = [IntegrationCheck(service="web", route="/"),
              IntegrationCheck(service="api", route="/api/x", method="POST")]
    IntegrationRunner(check_fn=fake).run(checks, {"web": "http://w", "api": "http://a"})
    assert ("http://w", "/") in seen and ("http://a", "/api/x") in seen
