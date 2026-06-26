"""Feishu INBOUND bot (MVP) — drop a PRD in Feishu, watch it build live, get the preview URL back.

A WebSocket long-connection (no public URL) subscribes to `im.message.receive_v1`. A text message
is treated as a PRD: the bot launches the existing pipeline (`python -m devagent.cli run --build`)
in a fresh runs dir and TAILS that run's `ledger.jsonl`, posting each phase/gate to the same chat as
it happens — then the deployed preview URL. The pipeline itself is untouched (the ledger is already
the event stream); this module only listens, spawns, tails, and replies.

Run it:  set FEISHU_APP_ID / FEISHU_APP_SECRET (+ ANTHROPIC_API_KEY for the build), then
    python -m devagent.channels.feishu_bot
Feishu app config required (console): enable Bot; scopes im:message + im:message:send_as_bot;
Event subscription in **long-connection** mode subscribed to "Receive messages" (im.message.receive_v1);
publish a version; add the bot to a group or open a DM.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import lark_oapi as lark

from . import feishu_app

_ROOT = Path(__file__).resolve().parents[2]          # the dev-agent dir (holds devagent/)
_MENTION = re.compile(r"@_user_\d+\s*")              # group @mention placeholders to strip
_POLL_S = 1.0


def _load_dotenv() -> None:
    """Minimal .env loader (the project doesn't use python-dotenv) so a bare run picks up creds."""
    env = _ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _format_event(ev: dict) -> str | None:
    """Map a ledger event to a friendly chat line (or None to stay silent)."""
    kind = ev.get("event")
    if kind == "phase":
        phase, ok = ev.get("phase"), ev.get("exit") == 0
        out = str(ev.get("output", ""))[:80]
        if not ok:
            return None  # the gate line (below) carries the failure detail
        return {
            "scope":  f"📋 Scope ✓ — {out}",
            "plan":   f"🗂️ Plan ✓ — {out}",
            "build":  "🔨 Build ✓ — rebuilt from source, booted, acceptance passed",
            "deploy": f"🚀 Deployed — {ev.get('meta', {}).get('url', '')}",
        }.get(phase)
    if kind == "gate" and not ev.get("ok"):
        return f"⛔ Gate failed at {ev.get('phase')}: {ev.get('reason', '')[:120]}"
    if kind == "run_end" and ev.get("status") != "succeeded":
        return f"❌ Run ended: {ev.get('status')} — {str(ev.get('detail', ''))[:120]}"
    return None


def _stream_run(api: lark.Client, chat_id: str, prd_text: str) -> None:
    """Run the pipeline on *prd_text* and stream its ledger to *chat_id* until completion."""
    runs = Path(tempfile.mkdtemp(prefix="feishu-run-"))
    prd = runs / "prd.md"
    prd.write_text(prd_text, encoding="utf-8")
    feishu_app.send_text(api, chat_id, "🛠️ Got it — starting an autonomous build. Scoping your request…")

    env = {**os.environ, "DEVAGENT_RUNS_DIR": str(runs)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "devagent.cli", "run", "--build", str(prd)],
        cwd=str(_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    ledger: Path | None = None
    seen = 0
    announced_build = False
    url: str | None = None

    def drain() -> None:
        nonlocal seen, announced_build, url
        if ledger is None or not ledger.exists():
            return
        lines = ledger.read_text(encoding="utf-8").splitlines()
        for ln in lines[seen:]:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue  # a half-written trailing line; picked up next poll
            msg = _format_event(ev)
            if msg:
                feishu_app.send_text(api, chat_id, msg)
            if ev.get("event") == "phase" and ev.get("phase") == "plan" and ev.get("exit") == 0 \
                    and not announced_build:
                announced_build = True
                feishu_app.send_text(api, chat_id,
                                     "🔨 Building + verifying (rebuild from source → boot → "
                                     "acceptance). This is the slow part — a few minutes…")
            if ev.get("event") == "phase" and ev.get("phase") == "deploy":
                url = ev.get("meta", {}).get("url") or url
        seen = len(lines)

    while True:
        if ledger is None:
            found = sorted(runs.glob("run-*"))
            if found:
                ledger = found[0] / "ledger.jsonl"
        drain()
        if proc.poll() is not None:
            drain()  # final flush after exit
            break
        time.sleep(_POLL_S)

    rc = proc.wait()
    if rc == 0 and url:
        feishu_app.send_text(api, chat_id,
                             f"✅ Done. Live preview: {url}\n"
                             "(opens on the host machine — public deploy is on the roadmap)")
    elif rc == 0:
        feishu_app.send_text(api, chat_id, "✅ Build finished.")
    else:
        clar = _scope_clarifications(runs)
        if clar:
            feishu_app.send_text(api, chat_id,
                                 "🤔 I need a bit more detail before building:\n"
                                 + "\n".join(f"• {q}" for q in clar)
                                 + "\n\nReply with the answers and I'll re-run.")
        else:
            feishu_app.send_text(api, chat_id, "❌ Build did not complete — see the run report on the host.")


def _scope_clarifications(runs: Path) -> list[str]:
    """The Scope phase's open questions, if it stopped to ask (so the ONE app bot can relay them
    — no separate outbound webhook bot needed)."""
    for rd in sorted(runs.glob("run-*")):
        sj = rd / "scope.json"
        if sj.is_file():
            try:
                return json.loads(sj.read_text(encoding="utf-8")).get("clarifications") or []
            except (ValueError, OSError):
                return []
    return []


def _make_handler(api: lark.Client):
    seen_messages: set[str] = set()                  # dedupe Feishu's at-least-once delivery

    def on_message(data: "lark.im.v1.P2ImMessageReceiveV1") -> None:
        msg = data.event.message
        if msg.message_id in seen_messages:
            return
        seen_messages.add(msg.message_id)
        if msg.message_type != "text":
            feishu_app.send_text(api, msg.chat_id,
                                 "Send me a text PRD (what to build) and I'll build it live. "
                                 "(PDF intake is on the roadmap.)")
            return
        text = _MENTION.sub("", json.loads(msg.content).get("text", "")).strip()
        # In a group, only act when the bot is @mentioned; in a DM, act on every message.
        if msg.chat_type != "p2p" and not msg.mentions:
            return
        if not text:
            return
        threading.Thread(target=_stream_run, args=(api, msg.chat_id, text), daemon=True).start()

    return on_message


def main() -> None:
    _load_dotenv()
    for required in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "ANTHROPIC_API_KEY"):
        if not os.environ.get(required):
            sys.exit(f"missing required env var: {required}")
    api = feishu_app.client()
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_make_handler(api))
               .build())
    ws = lark.ws.Client(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"],
                        event_handler=handler, log_level=lark.LogLevel.INFO)
    print("feishu bot: connecting (WebSocket long-connection)… drop a PRD in a DM or @mention me in a group.")
    ws.start()  # blocking


if __name__ == "__main__":
    main()
