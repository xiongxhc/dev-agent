"""M17 — cross-service integration verification. IntegrationRunner sequences a SystemDesign's
declared E2E flows against a {service -> base_url} map (each step addresses a named service, so
a flow can span services: frontend -> api -> db). The HTTP caller is injectable (tests pass a
fake); the default reuses acceptance_runner's check_api_json. Actual container bring-up that
supplies base_urls is deferred (needs Docker) and reuses the verifier's per-run-network pattern."""

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
    def __init__(self, check_fn=None):
        self.check_fn = check_fn      # (base_url, route, method, body, json_path, json_equals) -> {"ok",...}

    def run(self, checks: list[IntegrationCheck], base_urls: dict) -> IntegrationReport:
        fn = self.check_fn
        if fn is None:
            from .acceptance_runner import check_api_json as fn  # noqa: F811
        steps = []
        for c in checks:
            base = base_urls.get(c.service)
            if not base:
                steps.append({"service": c.service, "route": c.route, "ok": False,
                              "detail": f"no base_url for service {c.service!r}"})
                continue
            r = fn(base, c.route, c.method, c.body, c.json_path, c.json_equals)
            steps.append({"service": c.service, "route": c.route,
                          "ok": bool(r.get("ok")), "detail": r.get("detail", "")})
        return IntegrationReport(steps=steps)
