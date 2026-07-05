"""M17 — cross-service integration verification. IntegrationRunner sequences a SystemDesign's
declared E2E flows against a {service -> base_url} map (each step addresses a named service, so
a flow can span services: frontend -> api -> db). Both HTTP callers are injectable (tests pass
fakes); the defaults reuse acceptance_runner. Actual container bring-up that supplies base_urls
is deferred (needs Docker) and reuses the verifier's per-run-network pattern."""

from dataclasses import dataclass, field

from .schema import IntegrationCheck


@dataclass
class IntegrationReport:
    steps: list = field(default_factory=list)   # list of {"service","route","ok","detail"}

    @property
    def ok(self) -> bool:
        """True iff at least one step ran and every step passed. Zero steps => not ok:
        an integration verify that proved nothing must not read as success."""
        return bool(self.steps) and all(s["ok"] for s in self.steps)


class IntegrationRunner:
    """Sequences a SystemDesign's declared E2E flows against a {service -> base_url} map. A
    check WITH a json_path is a JSON assertion (check_api_json); a check WITHOUT one asserts the
    route simply responds with its expected_status (check_route_status) — so a non-JSON route
    (an HTML frontend at '/') passes. Both HTTP callers are injectable (tests pass fakes); the
    defaults reuse acceptance_runner. Container bring-up that supplies base_urls is deferred."""

    def __init__(self, route_status_fn=None, api_json_fn=None):
        self.route_status_fn = route_status_fn   # (base_url, route, expected_status) -> {"ok",...}
        self.api_json_fn = api_json_fn           # (base_url, route, method, body, json_path, json_equals) -> {"ok",...}

    def run(self, checks: list[IntegrationCheck], base_urls: dict) -> IntegrationReport:
        rs, aj = self.route_status_fn, self.api_json_fn
        if rs is None or aj is None:
            from .acceptance_runner import check_api_json, check_route_status
            rs = rs or check_route_status
            aj = aj or check_api_json
        steps = []
        for c in checks:
            base = base_urls.get(c.service)
            if not base:
                steps.append({"service": c.service, "route": c.route, "ok": False,
                              "detail": f"no base_url for service {c.service!r}"})
                continue
            # route_status probes with GET, so only a GET check without a JSON assertion may
            # fall back to it — a non-GET check (e.g. a derived POST with no declared response
            # schema) must still issue its real method via api_json (json_path None = any 2xx JSON).
            if c.json_path is not None or c.method.upper() != "GET":
                r = aj(base, c.route, c.method, c.body, c.json_path, c.json_equals)
            else:
                r = rs(base, c.route, c.expected_status)
            steps.append({"service": c.service, "route": c.route,
                          "ok": bool(r.get("ok")), "detail": r.get("detail", "")})
        return IntegrationReport(steps=steps)
