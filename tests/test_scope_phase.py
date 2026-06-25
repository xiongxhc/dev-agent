import json
from pathlib import Path

from devagent.phases.base import PhaseContext
from devagent.phases.scope import ScopePhase
from devagent.schema import ProjectScope


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


def _backend_only_payload():
    return {"title": "Notes API", "targets": [
        {"type": "backend", "stack": "node-express", "name": "api",
         "detail": {"endpoints": ["/api/notes"]},
         "acceptance_checks": [{"kind": "api_json", "route": "/api/notes", "json_path": "0.id"}]}],
        "clarifications": []}


def test_classifies_backend_only_not_frontend(tmp_path):
    prd = tmp_path / "p.md"; prd.write_text("a JSON API for notes, no UI")
    phase = ScopePhase(str(prd), client=_FakeLLM(_backend_only_payload()))
    res = phase.run(_ctx())
    assert res.exit_code == 0
    scope: ProjectScope = res.output_artifact
    assert [t.type for t in scope.targets] == ["backend"]  # did NOT collapse to frontend


def test_clarifications_block_with_exit_1_and_notify(tmp_path):
    prd = tmp_path / "p.md"; prd.write_text("build something")
    payload = {"title": "?", "targets": [
        {"type": "frontend", "stack": "node-vite-react", "name": "web",
         "detail": {}, "acceptance_checks": [{"kind": "route_status", "route": "/"}]}],
        "clarifications": ["What should it do?", "Any backend?"]}
    sent = []
    phase = ScopePhase(str(prd), client=_FakeLLM(payload), notifier=lambda t: sent.append(t))
    res = phase.run(_ctx())
    assert res.exit_code == 1
    assert res.output_artifact.clarifications
    assert sent and "What should it do?" in sent[0]


def test_answers_file_is_fed_into_prompt(tmp_path):
    prd = tmp_path / "p.md"; prd.write_text("build something")
    ans = tmp_path / "answers.txt"; ans.write_text("It's a notes API, backend only.")
    llm = _FakeLLM(_backend_only_payload())
    phase = ScopePhase(str(prd), answers_path=str(ans), client=llm)
    phase.run(_ctx())
    assert "notes API, backend only" in llm.seen_prompt
