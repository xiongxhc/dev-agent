from devagent.phases.architect import ArchitectPhase, _PROMPT, _UPDATE_PROMPT
from devagent.phases.base import PhaseContext
from devagent.schema import Contract, ServiceNode, SystemDesign


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload
        self.messages = self
        self.seen_prompt = None

    def create(self, **kw):
        self.seen_prompt = kw["messages"][0]["content"]
        block = type("B", (), {"type": "tool_use", "name": "emit", "input": self._payload})
        usage = type("U", (), {"input_tokens": 5, "output_tokens": 7})
        return type("R", (), {"content": [block], "usage": usage})


def _prior():
    return SystemDesign(
        title="Todos",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="UNIQUE-PRIOR-SLICE", provides=["api.openapi"])],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}}}})])


def _payload():
    return {"title": "Todos",
            "services": [{"id": "api", "name": "api", "kind": "backend",
                          "stack": "node-express", "prd_slice": "api with dark mode",
                          "provides": ["api.openapi"]}],
            "contracts": [{"id": "api.openapi", "kind": "openapi", "producer": "api",
                           "spec": {"paths": {"/api/todos": {"get": {}}}}}]}


def _ctx():
    return PhaseContext(sandbox=None, budget=None, ledger=None)


def test_update_mode_feeds_prior_design_and_change(tmp_path):
    change = tmp_path / "change.md"
    change.write_text("UNIQUE-CHANGE-REQUEST: add dark mode")
    llm = _FakeLLM(_payload())
    res = ArchitectPhase(str(change), client=llm, prior_design=_prior()).run(_ctx())
    assert res.exit_code == 0 and isinstance(res.output_artifact, SystemDesign)
    assert "UNIQUE-CHANGE-REQUEST" in llm.seen_prompt
    assert "UNIQUE-PRIOR-SLICE" in llm.seen_prompt          # serialized prior design
    assert "CURRENT SYSTEM DESIGN" in llm.seen_prompt


def test_fresh_mode_prompt_is_unchanged(tmp_path):
    prd = tmp_path / "p.md"
    prd.write_text("A todo app")
    llm = _FakeLLM(_payload())
    ArchitectPhase(str(prd), client=llm).run(_ctx())
    assert "REQUIREMENTS:" in llm.seen_prompt
    assert "CURRENT SYSTEM DESIGN" not in llm.seen_prompt


def test_update_prompt_pins_unchanged_service_names():
    text = _UPDATE_PROMPT.lower()
    assert "same `name`" in text and "keep" in text
    assert "db_schema" in text                    # warns that schema change resets data


def test_update_prompt_carries_the_base_design_rules():
    # the shared rules block is composed in, not duplicated-and-drifted
    for marker in ("acyclic", "bearerauth", "prd_slice"):
        assert marker in _UPDATE_PROMPT.lower()
    assert marker in _PROMPT.lower()
