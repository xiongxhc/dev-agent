"""Feishu notifier unit tests — urllib mocked, no network, no real webhook."""

import json
from pathlib import Path

from devagent.channels import feishu


class FakeResp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_notify_text_signs_and_posts(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode())
        return FakeResp(b'{"code":0,"msg":"success"}')

    monkeypatch.setattr(feishu.urllib.request, "urlopen", fake_urlopen)
    resp = feishu.notify_text("hi", webhook_url="https://example.com/hook", secret="s3cr3t")
    assert resp["code"] == 0
    p = captured["data"]
    assert p["msg_type"] == "text" and p["content"]["text"] == "hi"
    assert "sign" in p and "timestamp" in p  # signed when a secret is supplied
    assert captured["url"] == "https://example.com/hook"


def test_notify_text_without_secret_omits_sign(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=15):
        captured["data"] = json.loads(req.data.decode())
        return FakeResp(b'{"code":0}')

    monkeypatch.setattr(feishu.urllib.request, "urlopen", fake_urlopen)
    feishu.notify_text("hi", webhook_url="https://example.com/hook", secret=None)
    assert "sign" not in captured["data"] and "timestamp" not in captured["data"]


def test_sign_is_deterministic_and_base64():
    a = feishu._sign("1700000000", "secret")
    b = feishu._sign("1700000000", "secret")
    assert a == b and len(a) > 0
    import base64
    base64.b64decode(a)  # valid base64, else raises


def test_system_eta_bands():
    from devagent.channels.feishu_bot import _system_eta
    assert _system_eta(None) == "typically 5-20 min"
    assert _system_eta(1) == "typically 4-8 min"
    assert _system_eta(3) == "typically 8-15 min"
    assert _system_eta(5) == "typically 15-30 min"


def test_format_event_surfaces_system_level_arc():
    from devagent.channels.feishu_bot import _format_event
    # architect (system-level) is surfaced; a failing architect stays silent (end carries it)
    assert "Designing" in _format_event({"event": "phase", "phase": "architect", "exit": 0})
    assert _format_event({"event": "phase", "phase": "architect", "exit": 1}) is None
    # the build fan-out header names the services
    m = _format_event({"event": "system_build_start", "order": ["api", "web"]})
    assert "2 service" in m and "api" in m and "web" in m
    # per-node outcomes
    assert "api built" in _format_event({"event": "node", "node": "api", "status": "succeeded"})
    assert "web failed" in _format_event(
        {"event": "node", "node": "web", "status": "failed", "detail": "boom"})
    # M23 repair signal
    rep = _format_event({"event": "system_repair_start", "attempt": 1, "nodes": ["api"]})
    assert "repairing api" in rep and "attempt 1" in rep
    # M24 security not-run advisory
    assert "idor" in _format_event({"event": "security_not_run", "classes": ["idor"]})
    # preview URLs
    dep = _format_event({"event": "system_deploy", "urls": {"web": "http://web"}})
    assert "Preview up" in dep and "http://web" in dep
    # terminal failure surfaced; success handled by the completion message, not here
    assert "integration_failed" in _format_event(
        {"event": "system_build_end", "status": "integration_failed"})
    assert _format_event({"event": "system_build_end", "status": "succeeded"}) is None


def test_format_event_suppresses_per_service_subrun_noise():
    from devagent.channels.feishu_bot import _format_event
    # sub-run scope/plan/build/deploy phases, gates, run boundaries share the ledger and run
    # concurrently — they must NOT be streamed to chat (only the system-level arc is).
    assert _format_event({"event": "phase", "phase": "scope", "exit": 0}) is None
    assert _format_event({"event": "phase", "phase": "build", "exit": 0}) is None
    assert _format_event({"event": "gate", "phase": "build", "ok": False, "reason": "x"}) is None
    assert _format_event({"event": "run_start", "phases": ["scope"]}) is None
    assert _format_event({"event": "run_end", "status": "failed"}) is None
    assert _format_event({"event": "node", "node": "db", "status": "blocked"}) is None


def test_stream_run_invokes_build_system(monkeypatch, tmp_path):
    from devagent.channels import feishu_bot
    captured = {}

    class _FakeProc:
        def poll(self):
            return 0        # already exited: the stream loop drains once and breaks, no sleep

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(feishu_bot.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(feishu_bot.feishu_app, "send_text", lambda *a, **k: None)
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)

    feishu_bot._stream_run(api=None, chat_id="c", prd_text="build me a polls app")

    # The chat interface must drive the FULL pipeline (build-system), never the single-service
    # `run` path — that is the only flow carrying the M23 repair loop and M24 security phase.
    assert "build-system" in captured["cmd"]
    assert "run" not in captured["cmd"]
    assert captured["cmd"][:3] == [feishu_bot.sys.executable, "-m", "devagent.cli"]


def test_route_prefers_update_when_chat_has_an_app(monkeypatch, tmp_path):
    from devagent.channels import feishu_bot
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)
    app = tmp_path / "feishu-run-x" / "run-1"
    app.mkdir(parents=True)
    (app / "design.json").write_text("{}")
    feishu_bot._bind_chat_app("c1", str(app))

    assert feishu_bot._route("c1", "add a chart to the dashboard") == str(app)
    assert feishu_bot._route("c1", "start over — new app please") is None   # explicit escape
    assert feishu_bot._route("c2", "add a chart") is None                   # unknown chat
    # a bound dir whose design.json vanished (cleanup) falls back to a fresh build
    (app / "design.json").unlink()
    assert feishu_bot._route("c1", "add a chart") is None


def test_build_me_a_escape_only_fires_at_message_start(monkeypatch, tmp_path):
    from devagent.channels import feishu_bot
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)
    app = tmp_path / "feishu-run-x" / "run-1"
    app.mkdir(parents=True)
    (app / "design.json").write_text("{}")
    feishu_bot._bind_chat_app("c1", str(app))
    assert feishu_bot._route("c1", "build me a polls app") is None          # fresh build
    assert feishu_bot._route("c1", "can you build me a CSV export button") == str(app)  # update


def test_stream_run_routes_update_to_update_system(monkeypatch, tmp_path):
    from devagent.channels import feishu_bot
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)
    monkeypatch.setattr(feishu_bot.feishu_app, "send_text", lambda *a, **k: None)
    app = tmp_path / "feishu-run-x" / "run-1"
    app.mkdir(parents=True)
    (app / "design.json").write_text("{}")
    feishu_bot._bind_chat_app("c1", str(app))

    captured = {}

    class _FakeProc:
        def poll(self):
            return 0

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(feishu_bot.subprocess, "Popen", fake_popen)
    feishu_bot._stream_run(api=None, chat_id="c1", prd_text="make the header blue")

    assert "update-system" in captured["cmd"] and str(app) in captured["cmd"]
    assert (app / "change.md").read_text() == "make the header blue"


def test_successful_build_binds_the_chat_app(monkeypatch, tmp_path):
    import json as _json
    from devagent.channels import feishu_bot
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)
    monkeypatch.setattr(feishu_bot.feishu_app, "send_text", lambda *a, **k: None)

    class _FakeProc:
        def poll(self):
            return 0

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        runs = Path(cmd[-1]).parent            # the prd path sits in the mkdtemp'd runs dir
        run = runs / "run-1"
        run.mkdir(parents=True)
        (run / "ledger.jsonl").write_text(_json.dumps(
            {"event": "system_build_end", "status": "succeeded"}) + "\n")
        return _FakeProc()

    monkeypatch.setattr(feishu_bot.subprocess, "Popen", fake_popen)
    feishu_bot._stream_run(api=None, chat_id="c9", prd_text="an expense tracker for my team")
    bound = feishu_bot._load_chat_apps()["c9"]
    assert bound.endswith("run-1")


def test_format_event_update_arc():
    from devagent.channels.feishu_bot import _format_event
    m = _format_event({"event": "system_update_start", "changed": ["web"],
                       "schema_changed": False})
    assert "web" in m and "rebuilding" in m.lower()
    warn = _format_event({"event": "system_update_start", "changed": ["db", "api"],
                          "schema_changed": True})
    assert "data" in warn.lower() and "cleared" in warn.lower()
    none_changed = _format_event({"event": "system_update_start", "changed": [],
                                  "schema_changed": False})
    assert "re-verifying" in none_changed.lower()
    reused = _format_event({"event": "node", "node": "api", "status": "succeeded",
                            "detail": "unchanged: prior build reused"})
    assert "unchanged" in reused


def test_format_event_repo_events():
    from devagent.channels.feishu_bot import _format_event
    assert _format_event({"event": "repo", "url": "https://g/x"}) is None  # silent: URL rides the done message
    warn = _format_event({"event": "repo_error", "detail": "push rejected"})
    assert warn is not None and "push" in warn.lower() and "rejected" in warn


def test_tail_captures_repo_url(monkeypatch, tmp_path):
    from devagent.channels import feishu_bot
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        '{"event": "repo", "url": "https://gitlab.test/apps/x"}\n'
        '{"event": "system_deploy", "urls": {"api": "http://h:1"}}\n'
        '{"event": "system_build_end", "status": "succeeded"}\n')

    class DoneProc:
        def poll(self):
            return 0

        def wait(self):
            return 0

    sent = []
    monkeypatch.setattr(feishu_bot.feishu_app, "send_text",
                        lambda api, chat, msg: sent.append(msg))
    got_ledger, urls, status, repo_url = feishu_bot._tail(
        None, "chat-1", DoneProc(), lambda: ledger)
    assert repo_url == "https://gitlab.test/apps/x"
    assert urls == {"api": "http://h:1"} and status == "succeeded"


def test_concurrent_bindings_from_different_chats_all_survive(monkeypatch, tmp_path):
    import threading
    from devagent.channels import feishu_bot
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)

    threads = [
        threading.Thread(target=feishu_bot._bind_chat_app, args=(f"chat-{i}", f"/run/{i}"))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    apps = feishu_bot._load_chat_apps()
    assert apps == {f"chat-{i}": f"/run/{i}" for i in range(8)}


def test_bind_writes_atomically(monkeypatch, tmp_path):
    from devagent.channels import feishu_bot
    monkeypatch.setattr(feishu_bot, "_RUNS_BASE", tmp_path)

    feishu_bot._bind_chat_app("c", "/run/x")

    assert not list(tmp_path.glob("*.tmp"))
    apps = feishu_bot._load_chat_apps()
    assert apps == {"c": "/run/x"}


def test_help_intents_get_the_usage_card():
    from devagent.channels.feishu_bot import _help_reply
    for t in ("help", "Help?", "/help", "?", "faq", "usage", "hi", "Hello!",
              "what can you do", "What features are there?", "How do I use this?",
              "帮助", "怎么用", "你能做什么？"):
        assert _help_reply(t), t


def test_chained_help_intents_get_the_usage_card():
    # live miss 2026-07-14: "What can you do and how to use" started a real build
    from devagent.channels.feishu_bot import _help_reply
    for t in ("What can you do and how to use",
              "help, what can you do?",
              "hi, how do I use this bot?",
              "faq / usage",
              "你能做什么，怎么用？"):
        assert _help_reply(t), t


def test_requirements_are_never_mistaken_for_help():
    from devagent.channels.feishu_bot import _help_reply
    for t in ("build a helpdesk app for IT support",
              "an expense tracker with usage reports",
              "add a help page to the app",
              "hi-priority ticket queue for the ops team",
              "make the FAQ section collapsible",
              # chained-looking messages where a segment is NOT a help phrase stay builds
              "what can you do about slow queries in my dashboard app",
              "help me build an expense tracker",
              "hi, build me a polls app"):
        assert _help_reply(t) is None, t
