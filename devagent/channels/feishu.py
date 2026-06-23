"""Feishu (Lark) outbound notifier — the NOTIFICATION half of the group channel.

Uses a Feishu *custom-bot incoming webhook*, which is OUTBOUND ONLY: it posts messages
INTO a group. It cannot receive messages, @mentions, or file uploads — the TRIGGER /
CONVERSE half needs a full Feishu app with event subscriptions (see the channel design
note). Credentials come from env (FEISHU_WEBHOOK_URL, FEISHU_SECRET); never commit them.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request


def _sign(timestamp: str, secret: str) -> str:
    # Feishu custom-bot signing: HMAC-SHA256 with key = f"{timestamp}\n{secret}",
    # empty message body, then base64.
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def notify_text(text: str, *, webhook_url: str | None = None,
                secret: str | None = None, timeout: int = 15) -> dict:
    """Post a plain-text message to the group. Returns Feishu's JSON response
    ({"code": 0, ...} on success)."""
    webhook_url = webhook_url or os.environ["FEISHU_WEBHOOK_URL"]
    secret = secret if secret is not None else os.getenv("FEISHU_SECRET")

    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(ts, secret)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
