"""Feishu INBOUND bot — drop a requirement in Feishu, watch it build live, get the preview URL back.

A WebSocket long-connection (no public URL) subscribes to `im.message.receive_v1`. A text message
is treated as a requirement and handed to the full multi-service pipeline
(`python -m devagent.cli build-system`): the architect decides how many services it is, each is
built + verified, the system is brought up and cross-service integration-checked, the security
verify phase red-teams the preview, and any gating failure drives the system repair loop — then the
deployed preview URL(s) come back. The bot TAILS that run's `ledger.jsonl` and posts each milestone
to the same chat as it happens. The pipeline itself is untouched (the ledger is already the event
stream); this module only listens, spawns, tails, and replies.

Always `build-system`, never the single-service `run` path: `build-system` is the superset (a
one-service requirement yields a one-node design) and it is the ONLY flow that carries the system
repair loop (M23) and the security verify phase (M24). Routing chat requests anywhere else would
silently skip both — the chat is the product interface, so it gets the full pipeline.

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
_HEARTBEAT_S = 180.0     # progress ping cadence during the long build phase
# Stable, browsable runs root (override with DEVAGENT_FEISHU_RUNS_DIR). Same dir every other
# entrypoint uses (CLI `run --build`, `build-system`) — one place to look for any run,
# gitignored so builds never land in the repo. Each build still gets a unique subdir, under a
# predictable path so you can `tail -f` the ledger and open the run report — unlike a random
# system tempdir that's unfindable and OS-cleaned mid-preview.
_RUNS_BASE = Path(os.environ.get("DEVAGENT_FEISHU_RUNS_DIR", _ROOT / "runs"))


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


def _system_eta(n_services: int | None) -> str:
    """Honest band estimate for a whole system build (architect → per-service build+verify →
    bring-up → integration → security verify → any repair pass), sized by service count — the
    only sizing signal available once the architect has designed the system. Bands from observed
    live runs: a single-service app ≈ 4-8 min; a small multi-service system runs well past 10."""
    if not n_services:
        return "typically 5-20 min"
    if n_services <= 1:
        return "typically 4-8 min"
    if n_services <= 3:
        return "typically 8-15 min"
    return "typically 15-30 min"


def _format_event(ev: dict) -> str | None:
    """Map a system-build ledger event to a friendly chat line (or None to stay silent).

    The `build-system` ledger interleaves system-level events with each per-service sub-run's own
    scope/plan/build/deploy/gate/run_start/run_end events (they share one ledger, and siblings run
    concurrently). Streaming the sub-run events would be a confusing concurrent jumble, so this
    surfaces only the system-level arc — architect → building N services → each node done →
    repair (if verification failed) → security note → preview — and the per-node build outcomes.
    A failed service's `node` event carries the detail, so sub-run gate/phase noise is suppressed."""
    kind = ev.get("event")
    if kind == "phase" and ev.get("phase") == "architect":
        # Only the architect phase runs at the system level; every other `phase` event is a
        # concurrent sub-run's and is suppressed. Architect failure surfaces via system_build_end.
        return "🏗️ Designing the system architecture…" if ev.get("exit") == 0 else None
    if kind == "system_build_start":
        order = ev.get("order") or []
        n = len(order)
        return (f"🧩 Architecture ready — building {n} service{'s' if n != 1 else ''}: "
                f"{', '.join(order)}")
    if kind == "node":
        node, status = ev.get("node"), ev.get("status")
        if status == "succeeded":
            return f"  ✅ {node} built"
        if status == "failed":
            return f"  ❌ {node} failed — {str(ev.get('detail', ''))[:100]}"
        return None  # blocked: a dependency failed; that failing node already reported
    if kind == "system_repair_start":
        nodes = ", ".join(ev.get("nodes") or [])
        return (f"🔧 Verification failed — repairing {nodes} "
                f"(attempt {ev.get('attempt')}), then re-verifying…")
    if kind == "security_not_run":
        classes = ", ".join(ev.get("classes") or [])
        return (f"🔐 Note: {classes} security probe(s) did not run (no second principal available) "
                "— a coverage gap, not a clean pass.")
    if kind == "system_deploy":
        urls = ev.get("urls") or {}
        if not urls:
            return None
        body = "\n".join(f"  • {sid}: {url}" for sid, url in urls.items())
        return f"🚀 Preview up:\n{body}"
    if kind == "system_build_end" and ev.get("status") != "succeeded":
        return f"❌ Build ended: {ev.get('status')}"
    return None


def _stream_run(api: lark.Client, chat_id: str, prd_text: str) -> None:
    """Run the full `build-system` pipeline on *prd_text* and stream its ledger to *chat_id*."""
    _RUNS_BASE.mkdir(parents=True, exist_ok=True)
    runs = Path(tempfile.mkdtemp(prefix="feishu-run-", dir=str(_RUNS_BASE)))
    prd = runs / "prd.md"
    prd.write_text(prd_text, encoding="utf-8")
    feishu_app.send_text(api, chat_id,
                         "🛠️ Got it — starting an autonomous system build. Designing the architecture…")

    env = {**os.environ, "DEVAGENT_RUNS_DIR": str(runs)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "devagent.cli", "build-system", str(prd)],
        cwd=str(_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    ledger: Path | None = None
    seen = 0
    announced = False
    build_started_at: float | None = None
    build_done = False
    eta = _system_eta(None)
    urls: dict = {}
    status: str | None = None

    def drain() -> None:
        nonlocal seen, announced, build_started_at, build_done, eta, urls, status
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
            kind = ev.get("event")
            if kind == "system_build_start" and not announced:
                announced = True
                build_started_at = time.monotonic()
                eta = _system_eta(len(ev.get("order") or []))
                feishu_app.send_text(api, chat_id,
                                     "🔨 Building + verifying each service, then wiring, "
                                     "integration-testing and security-checking the system — "
                                     f"the slow part ({eta}). I'll post progress as I go.")
            if kind == "system_deploy":
                urls = dict(ev.get("urls") or {})
            if kind == "system_build_end":
                status = ev.get("status")
                build_done = True
        seen = len(lines)

    last_heartbeat = time.monotonic()
    while True:
        if ledger is None:
            found = sorted(runs.glob("run-*"))
            if found:
                ledger = found[0] / "ledger.jsonl"
        drain()
        # Heartbeat while the long build phase runs, so the chat is never silent for many minutes.
        if announced and not build_done \
                and time.monotonic() - last_heartbeat >= _HEARTBEAT_S:
            last_heartbeat = time.monotonic()
            mins = int((time.monotonic() - (build_started_at or last_heartbeat)) // 60)
            feishu_app.send_text(api, chat_id,
                                 f"⏳ Still building — {mins} min elapsed ({eta}).")
        if proc.poll() is not None:
            drain()  # final flush after exit
            break
        time.sleep(_POLL_S)

    proc.wait()
    if urls:
        body = "\n".join(f"• {sid}: {u}" for sid, u in urls.items())
        feishu_app.send_text(api, chat_id,
                             "✅ Done — system built, verified and security-checked. Live preview:\n"
                             f"{body}\n(opens on the host machine — public deploy is on the roadmap)")
    elif status == "succeeded":
        feishu_app.send_text(api, chat_id, "✅ System build finished.")
    else:
        feishu_app.send_text(api, chat_id,
                             f"❌ Build did not complete — status: {status or 'unknown'}. "
                             "See the run report on the host.")


def _make_handler(api: lark.Client):
    seen_messages: set[str] = set()                  # dedupe Feishu's at-least-once delivery

    def on_message(data: "lark.im.v1.P2ImMessageReceiveV1") -> None:
        msg = data.event.message
        if msg.message_id in seen_messages:
            return
        seen_messages.add(msg.message_id)
        if msg.message_type != "text":
            feishu_app.send_text(api, msg.chat_id,
                                 "Send me a text requirement (what to build) and I'll design, "
                                 "build and security-check it live. (PDF intake is on the roadmap.)")
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
    _lvl = getattr(lark.LogLevel, os.environ.get("FEISHU_LOG_LEVEL", "INFO").upper(),
                   lark.LogLevel.INFO)
    ws = lark.ws.Client(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"],
                        event_handler=handler, log_level=_lvl)
    print("feishu bot: connecting (WebSocket long-connection)… drop a requirement in a DM or @mention me in a group.")
    ws.start()  # blocking


if __name__ == "__main__":
    main()
