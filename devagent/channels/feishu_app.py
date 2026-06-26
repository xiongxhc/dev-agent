"""Feishu APP messaging — the INBOUND app's outbound replies.

Distinct from `feishu.py` (a one-way custom-bot *group webhook*): this sends into a specific
chat using the Feishu app's tenant token, which `lark_oapi` mints and refreshes automatically
from FEISHU_APP_ID / FEISHU_APP_SECRET. Used by `feishu_bot.py` to reply in the chat a message
arrived from (live build progress + the final preview URL)."""

import json
import os
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody


def client() -> lark.Client:
    """An app API client; auto-manages the tenant_access_token."""
    return (lark.Client.builder()
            .app_id(os.environ["FEISHU_APP_ID"])
            .app_secret(os.environ["FEISHU_APP_SECRET"])
            .build())


def send_text(cli: lark.Client, chat_id: str, text: str) -> None:
    """Post a plain-text message into *chat_id*. Best-effort: a send failure is logged, never
    raised — a dropped progress line must not abort the build it is reporting on."""
    req = (CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(chat_id)
                         .msg_type("text")
                         .content(json.dumps({"text": text}))
                         .build())
           .build())
    resp = cli.im.v1.message.create(req)
    if not resp.success():
        print(f"feishu send failed: code={resp.code} msg={resp.msg}", file=sys.stderr)
