"""ManagedExecutor (A/B arm B) — the Managed Agents client is faked (no API, no spend), but
the tarball round-trip (download -> extract -> files on disk) is exercised for real."""

import io
import tarfile

from devagent.executor import BuildRequest
from devagent.managed_executor import ManagedExecutor
from devagent.schema import AcceptanceCheck, Plan, Spec, Task


def _req(workdir):
    spec = Spec(title="Hello", pages=["/"],
                acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])
    plan = Plan(tasks=[Task(id="a", description="scaffold", owned_files=["package.json"])])
    return BuildRequest(spec=spec, plan=plan, workdir=str(workdir), run_id="r1")


def _tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Stream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, *a):
        return False


class FakeClient:
    """Mimics anthropic's client.beta.{agents,environments,sessions,files} surface."""

    def __init__(self, tar_bytes, *, list_files=None):
        self.tar = tar_bytes
        self.calls = {"sent": [], "deleted": []}
        outer = self
        files_listing = [_Obj(id="file_1", filename="app.tar.gz")] if list_files is None else list_files

        class Agents:
            def create(self, **kw):
                outer.calls["agent"] = kw
                return _Obj(id="agent_1", version=1)

        class Environments:
            def create(self, **kw):
                outer.calls["env"] = kw
                return _Obj(id="env_1")

        class Events:
            def stream(self, session_id):
                return _Stream([_Obj(type="agent.tool_use", name="bash"),
                                _Obj(type="session.status_idle")])

            def send(self, session_id, events=None):
                outer.calls["sent"].append(events)

        class Files:
            def list(self, scope_id=None, betas=None):
                outer.calls["list"] = {"scope_id": scope_id, "betas": betas}
                return _Obj(data=files_listing)

            def download(self, file_id, betas=None):
                return _Obj(read=lambda: outer.tar)

        class Sessions:
            events = Events()

            def create(self, **kw):
                outer.calls["session"] = kw
                return _Obj(id="sesn_1")

            def delete(self, session_id):
                outer.calls["deleted"].append(session_id)

        class Beta:
            agents = Agents()
            environments = Environments()
            sessions = Sessions()
            files = Files()

        self.beta = Beta()


def _exec(client):
    return ManagedExecutor(client=client, poll_attempts=1, poll_delay=0)


def test_builds_and_extracts_tarball_into_workdir(tmp_path):
    out = tmp_path / "out"
    tar = _tar({"package.json": "{}", "pnpm-lock.yaml": "lock",
                "src/App.tsx": "x", "dist/index.html": "<html></html>"})
    fake = FakeClient(tar)
    res = _exec(fake).build(_req(out))

    assert res.success is True
    assert (out / "dist" / "index.html").is_file()
    assert (out / "src" / "App.tsx").is_file()   # directory tree survived the round-trip
    assert (out / "pnpm-lock.yaml").is_file()


def test_prompt_carries_spec_and_plan_and_agent_uses_toolset(tmp_path):
    fake = FakeClient(_tar({"dist/index.html": "<html></html>"}))
    _exec(fake).build(_req(tmp_path / "out"))
    # agent created with the managed-agents toolset
    assert fake.calls["agent"]["tools"] == [{"type": "agent_toolset_20260401"}]
    # the user message embeds the Spec (title) + the /mnt/session/outputs tar instruction
    sent_text = fake.calls["sent"][0][0]["content"][0]["text"]
    assert "Hello" in sent_text and "/mnt/session/outputs" in sent_text and "app.tar.gz" in sent_text
    # files.list scoped to the session, with the managed-agents beta
    assert fake.calls["list"]["betas"] == ["managed-agents-2026-04-01"]


def test_failure_when_no_tarball_in_outputs(tmp_path):
    fake = FakeClient(b"", list_files=[])  # session produced no app.tar.gz
    res = _exec(fake).build(_req(tmp_path / "out"))
    assert res.success is False
    assert "app.tar.gz" in (res.error or "")


def test_failure_when_tarball_has_no_dist(tmp_path):
    fake = FakeClient(_tar({"package.json": "{}"}))  # built tree but no dist/index.html
    res = _exec(fake).build(_req(tmp_path / "out"))
    assert res.success is False
    assert "dist" in (res.error or "")


def test_writes_spec_and_plan_for_shared_acceptance(tmp_path):
    # The shared acceptance runner reads out/.devagent/spec.json; the managed arm must drop it
    # too (SdkExecutor does). Regression for the first live run's acceptance crash.
    out = tmp_path / "out"
    _exec(FakeClient(_tar({"dist/index.html": "<html></html>"}))).build(_req(out))
    assert (out / ".devagent" / "spec.json").is_file()
    assert (out / ".devagent" / "plan.json").is_file()
    import json
    assert json.loads((out / ".devagent" / "spec.json").read_text())["title"] == "Hello"


def test_session_is_always_deleted(tmp_path):
    fake = FakeClient(_tar({"dist/index.html": "<html></html>"}))
    _exec(fake).build(_req(tmp_path / "out"))
    assert fake.calls["deleted"] == ["sesn_1"]  # cleanup ran (stops session-hour billing)
