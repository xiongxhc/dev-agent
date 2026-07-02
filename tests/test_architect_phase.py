from devagent.phases.base import PhaseContext
from devagent.phases.architect import ArchitectPhase
from devagent.schema import SystemDesign


class _FakeLLM:
    """Stands in for anthropic.Anthropic — returns a forced tool_use 'emit' block."""
    def __init__(self, payload):
        self._payload = payload
        self.messages = self
        self.seen_prompt = None

    def create(self, **kw):
        self.seen_prompt = kw["messages"][0]["content"]
        block = type("B", (), {"type": "tool_use", "name": "emit", "input": self._payload})
        usage = type("U", (), {"input_tokens": 5, "output_tokens": 7})
        return type("R", (), {"content": [block], "usage": usage})


def _ctx():
    return PhaseContext(sandbox=None, budget=None, ledger=None)


def _two_service_payload():
    return {
        "title": "Todo system",
        "services": [
            {"id": "api", "name": "api", "kind": "backend", "stack": "node-express",
             "prd_slice": "A JSON API for todos.", "provides": ["api.openapi"]},
            {"id": "web", "name": "web", "kind": "frontend", "stack": "node-vite-react",
             "prd_slice": "A UI listing todos.", "depends_on": ["api"], "consumes": ["api.openapi"]},
        ],
        "contracts": [{"id": "api.openapi", "kind": "openapi", "producer": "api",
                       "spec": {"paths": {"/api/todos": {"get": {}}}}}],
    }


def test_emits_system_design_from_prd(tmp_path):
    prd = tmp_path / "p.md"; prd.write_text("A todo app with a UI and an API.")
    phase = ArchitectPhase(str(prd), client=_FakeLLM(_two_service_payload()))
    res = phase.run(_ctx())
    assert res.exit_code == 0
    design = res.output_artifact
    assert isinstance(design, SystemDesign)
    assert [s.id for s in design.services] == ["api", "web"]
    assert res.meta["tokens_in"] == 5


def test_prd_text_is_fed_into_prompt(tmp_path):
    prd = tmp_path / "p.md"; prd.write_text("UNIQUE-MARKER-PRD-TEXT")
    llm = _FakeLLM(_two_service_payload())
    ArchitectPhase(str(prd), client=llm).run(_ctx())
    assert "UNIQUE-MARKER-PRD-TEXT" in llm.seen_prompt


def test_prompt_instructs_agent_to_design_the_services():
    from devagent.phases.architect import _PROMPT
    text = _PROMPT.lower()
    assert "you decide the service decomposition" in text
    assert "depends_on" in text and "acyclic" in text and "prd_slice" in text


def test_invalid_payload_returns_exit_1(tmp_path):
    prd = tmp_path / "p.md"; prd.write_text("x")
    bad = {"title": "t", "services": []}  # zero services -> ValidationError inside generate_structured
    res = ArchitectPhase(str(prd), client=_FakeLLM(bad)).run(_ctx())
    assert res.exit_code == 1
