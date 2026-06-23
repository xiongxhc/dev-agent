"""Feishu notifier unit tests — urllib mocked, no network, no real webhook."""

import json

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
