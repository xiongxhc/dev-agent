from devagent.schema import IntegrationCheck
from devagent.integration import IntegrationRunner, IntegrationReport


def test_all_steps_pass():
    checks = [IntegrationCheck(service="api", route="/api/todos"),
              IntegrationCheck(service="web", route="/")]
    rep = IntegrationRunner(route_status_fn=lambda *a, **k: {"ok": True}).run(
        checks, {"api": "http://api", "web": "http://web"})
    assert isinstance(rep, IntegrationReport) and rep.ok
    assert [s["service"] for s in rep.steps] == ["api", "web"]


def test_a_failing_step_fails_the_report():
    checks = [IntegrationCheck(service="api", route="/api/todos")]
    rep = IntegrationRunner(route_status_fn=lambda *a, **k: {"ok": False, "detail": "500"}).run(
        checks, {"api": "http://api"})
    assert not rep.ok and rep.steps[0]["detail"] == "500"


def test_missing_base_url_is_a_failed_step():
    rep = IntegrationRunner(route_status_fn=lambda *a, **k: {"ok": True}).run(
        [IntegrationCheck(service="api", route="/x")], {})
    assert not rep.ok and not rep.steps[0]["ok"]


def test_empty_checks_is_not_ok():
    rep = IntegrationRunner(route_status_fn=lambda *a, **k: {"ok": True}).run([], {"api": "http://api"})
    assert not rep.ok


def test_no_json_path_uses_route_status_with_expected_status():
    seen = {}
    def rs(base_url, route, expected_status):
        seen["args"] = (base_url, route, expected_status); return {"ok": True}
    def aj(*a, **k):
        seen["aj_called"] = True; return {"ok": True}
    checks = [IntegrationCheck(service="web", route="/", expected_status=200)]
    IntegrationRunner(route_status_fn=rs, api_json_fn=aj).run(checks, {"web": "http://w"})
    assert seen["args"] == ("http://w", "/", 200)          # route_status got expected_status
    assert "aj_called" not in seen                          # api_json NOT used for a no-json_path check


def test_json_path_uses_api_json():
    seen = {}
    def rs(*a, **k):
        seen["rs_called"] = True; return {"ok": True}
    def aj(base_url, route, method, body, json_path, json_equals):
        seen["args"] = (base_url, route, method, json_path); return {"ok": True}
    checks = [IntegrationCheck(service="api", route="/api/todos", method="POST",
                               body={"t": 1}, json_path="id")]
    IntegrationRunner(route_status_fn=rs, api_json_fn=aj).run(checks, {"api": "http://a"})
    assert seen["args"] == ("http://a", "/api/todos", "POST", "id")   # api_json got the check
    assert "rs_called" not in seen                         # route_status NOT used for a json_path check
