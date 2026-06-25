# tests/test_plan_multi.py
from devagent.phases.base import PhaseContext
from devagent.phases.plan import PlanPhase
from devagent.schema import AcceptanceCheck, ArtifactSpec, ProjectScope


class _FakeLLM:
    def __init__(self, payload): self._p = payload; self.messages = self; self.seen = None
    def create(self, **kw):
        self.seen = kw["messages"][0]["content"]
        b = type("B", (), {"type": "tool_use", "name": "emit", "input": self._p})
        u = type("U", (), {"input_tokens": 1, "output_tokens": 1})
        return type("R", (), {"content": [b], "usage": u})


def test_plan_prompt_mentions_every_target():
    scope = ProjectScope(title="App", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                     detail={"pages": ["/"]},
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")]),
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     detail={"endpoints": ["/api/x"]},
                     acceptance_checks=[AcceptanceCheck(kind="api_json", route="/api/x", json_path="ok")]),
    ])
    payload = {"tasks": [
        {"id": "web", "description": "frontend", "owned_files": ["web/src/App.tsx"]},
        {"id": "api", "description": "backend", "owned_files": ["api/src/server.ts"]},
    ]}
    llm = _FakeLLM(payload)
    ctx = PhaseContext(sandbox=None, budget=None, ledger=None, artifacts={"scope": scope})
    res = PlanPhase(client=llm).run(ctx)
    assert res.exit_code == 0
    assert "web" in llm.seen and "api" in llm.seen
