"""Feishu INBOUND bot — drop a requirement in Feishu, watch it build live, get the preview URL back.

A WebSocket long-connection (no public URL) subscribes to `im.message.receive_v1`. A text message
is treated as a requirement and handed to the full multi-service pipeline
(`python -m devagent.cli build-system`): the architect decides how many services it is, each is
built + verified, the system is brought up and cross-service integration-checked, the security
verify phase red-teams the preview, and any gating failure drives the system repair loop — then the
deployed preview URL(s) come back. The bot TAILS that run's `ledger.jsonl` and posts each milestone
to the same chat as it happens. The pipeline itself is untouched (the ledger is already the event
stream); this module only listens, spawns, tails, and replies. A help/greeting message ("help",
"what can you do?", "怎么用") gets a usage card instead of triggering a build.

M25: chat state carries across messages. Once a chat's build succeeds, the bot binds that chat to
the app's run dir; a follow-up message in the same chat is routed to `update-system` against that
run dir instead of starting a fresh build. Say "start over" (or "new app", "from scratch", …) to
escape back to a fresh build.

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

_CHAT_APPS = "chat-apps.json"        # chat_id -> the inner run dir of the chat's latest app

# Explicit new-app escapes; anything else in a chat WITH a prior app updates that app (M25).
# "build me a" only counts at the message start — mid-sentence ("can you build me a CSV
# export button") is an update request, not a fresh-build escape.
_NEW_APP = re.compile(
    r"(new app|start over|from scratch|start fresh|新的?应用|重新开始|从头)|^\s*build me a", re.I)

# A lost user's "help" must NOT become a build: full-match help/greeting intents only,
# anchored so real requirements that merely contain these words ("build a helpdesk app",
# "an expense tracker with usage reports") never match. Intents may be CHAINED by plain
# connectors ("What can you do and how to use" — live miss, 2026-07-14): every chained
# segment must itself be a help phrase, so the anchor-safety is unchanged.
_HELP_PHRASE = (
    r"(help|/help|\?|？|faq|usage|hi|hello|hey|"
    r"what (can|do) you do|what features?( are there| do you have)?|"
    r"how (do i|to) use( this| it)?( bot)?|"
    r"帮助|怎么用|使用说明|你能做什么|你会什么)")
_HELP_CONNECTOR = r"(\s*[,，/&、]\s*|\s+(and|or)\s+|\s*(以及|和|跟)\s*)"
_HELP = re.compile(
    rf"^\s*{_HELP_PHRASE}({_HELP_CONNECTOR}{_HELP_PHRASE})*\s*[?？!！。.]*\s*$", re.I)

_HELP_CARD = """\
🤖 I turn plain-language requirements into working software, right here in chat.

• Build: describe the app you want (e.g. "an expense tracker for my team — employees submit, managers approve"). I design the architecture, build + verify each service, security-check it, and post a live preview URL (typically 5-20 min).
• Update: once this chat has built an app, just reply with changes ("make the header blue", "add a CSV export"). I update the app in place — your data survives unless the change alters the data model (I warn you first).
• Start fresh: say "start over" or "new app" to leave this chat's app behind and build a new one.
• In a group, @mention me; in a DM, just type.

On the roadmap: PDF requirements intake, public deploys, data-preserving schema migrations."""


def _help_reply(text: str) -> str | None:
    """The usage card for a help/greeting intent, else None (the message is a requirement)."""
    return _HELP_CARD if _HELP.match(text or "") else None


_chat_locks: dict[str, threading.Lock] = {}
_chat_locks_guard = threading.Lock()
# _chat_lock(chat_id) only serializes messages within the SAME chat; the bot spawns a thread per
# inbound message regardless of chat, so two different chats' builds finishing concurrently can
# still interleave load->mutate->write on the one shared chat-apps.json. This dedicated guard
# serializes ALL writers to that file across chats.
_chat_apps_guard = threading.Lock()


def _chat_lock(chat_id: str) -> threading.Lock:
    with _chat_locks_guard:
        return _chat_locks.setdefault(chat_id, threading.Lock())


def _chat_apps_path() -> Path:
    # derived per call (never module-level) so tests monkeypatching _RUNS_BASE take effect
    return _RUNS_BASE / _CHAT_APPS


def _load_chat_apps() -> dict:
    try:
        return json.loads(_chat_apps_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _bind_chat_app(chat_id: str, run_dir: str) -> None:
    with _chat_apps_guard:
        apps = _load_chat_apps()
        apps[chat_id] = run_dir
        _RUNS_BASE.mkdir(parents=True, exist_ok=True)
        # Write to a same-dir temp file then atomically replace, so a crash mid-write can't
        # corrupt chat-apps.json — _load_chat_apps' broad except would otherwise treat every
        # chat as unbound.
        path = _chat_apps_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _route(chat_id: str, text: str) -> str | None:
    """The prior run dir this message should UPDATE, or None for a fresh build. Update iff
    the chat already built an app (still on disk) and the text doesn't ask to start over."""
    if _NEW_APP.search(text):
        return None
    run_dir = _load_chat_apps().get(chat_id)
    if run_dir and (Path(run_dir) / "design.json").is_file():
        return run_dir
    return None


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
            if str(ev.get("detail", "")).startswith("unchanged"):
                return f"  ↩️ {node} unchanged — reused"
            return f"  ✅ {node} built"
        if status == "failed":
            return f"  ❌ {node} failed — {str(ev.get('detail', ''))[:100]}"
        return None  # blocked: a dependency failed; that failing node already reported
    if kind == "system_repair_start":
        nodes = ", ".join(ev.get("nodes") or [])
        return (f"🔧 Verification failed — repairing {nodes} "
                f"(attempt {ev.get('attempt')}), then re-verifying…")
    if kind == "system_update_start":
        changed = ", ".join(ev.get("changed") or [])
        head = (f"🔁 Change mapped to: {changed} — rebuilding just those service(s)…"
                if changed else
                "🔁 No service needs rebuilding — re-verifying and redeploying…")
        if ev.get("schema_changed"):
            head += ("\n⚠️ This change alters the data model — existing data will be "
                     "cleared. (Data-preserving migrations are on the roadmap.)")
        return head
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


def _tail(api, chat_id, proc, find_ledger, seen: int = 0):
    """Tail the run's ledger while `proc` runs, posting each system-level event to chat.
    `find_ledger()` resolves the ledger path (None until it exists — the build path globs
    for the run dir the CLI creates). `seen` skips lines already present BEFORE this run
    (an update appends to the prior run's ledger). Returns (ledger_path, urls, status)."""
    ledger: Path | None = None
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
            ledger = find_ledger()
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
    return ledger, urls, status


def _stream_build(api: lark.Client, chat_id: str, prd_text: str) -> None:
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

    def find_ledger() -> Path | None:
        found = sorted(runs.glob("run-*"))
        return (found[0] / "ledger.jsonl") if found else None

    ledger, urls, status = _tail(api, chat_id, proc, find_ledger)
    if status == "succeeded" and ledger is not None:
        # this run dir (design.json + services/) IS the chat's app state; follow-ups update it
        _bind_chat_app(chat_id, str(ledger.parent))
    if urls:
        body = "\n".join(f"• {sid}: {u}" for sid, u in urls.items())
        feishu_app.send_text(api, chat_id,
                             "✅ Done — system built, verified and security-checked. Live preview:\n"
                             f"{body}\n(opens on the host machine — public deploy is on the roadmap)\n"
                             "Reply in this chat with changes and I'll update the app in place.")
    elif status == "succeeded":
        feishu_app.send_text(api, chat_id,
                             "✅ System build finished.\n"
                             "Reply in this chat with changes and I'll update the app in place.")
    else:
        feishu_app.send_text(api, chat_id,
                             f"❌ Build did not complete — status: {status or 'unknown'}. "
                             "See the run report on the host.")


def _stream_update(api, chat_id, change_text: str, run_dir: Path) -> None:
    """M25 update: run `update-system` against the chat's bound run dir and stream the
    SAME (appended) ledger from where the prior run left off."""
    change = run_dir / "change.md"
    change.write_text(change_text, encoding="utf-8")
    ledger_path = run_dir / "ledger.jsonl"
    seen = (len(ledger_path.read_text(encoding="utf-8").splitlines())
            if ledger_path.exists() else 0)
    feishu_app.send_text(api, chat_id,
                         "🔁 Got it — updating the app built in this chat. "
                         "Re-designing around your change…")
    proc = subprocess.Popen(
        [sys.executable, "-m", "devagent.cli", "update-system", str(run_dir), str(change)],
        cwd=str(_ROOT), env={**os.environ},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _, urls, status = _tail(api, chat_id, proc, lambda: ledger_path, seen=seen)
    if urls:
        body = "\n".join(f"• {sid}: {u}" for sid, u in urls.items())
        feishu_app.send_text(api, chat_id,
                             f"✅ Updated, re-verified and redeployed. Live preview:\n{body}")
    elif status == "succeeded":
        feishu_app.send_text(api, chat_id, "✅ Update finished.")
    else:
        feishu_app.send_text(api, chat_id,
                             f"❌ Update did not complete — status: {status or 'unknown'}. "
                             "Say \"start over\" to rebuild from scratch.")


def _stream_run(api: lark.Client, chat_id: str, prd_text: str) -> None:
    """Per-message entry: route new-vs-update, serialized per chat (two rapid messages
    must not race the same app's run dir)."""
    with _chat_lock(chat_id):
        prior = _route(chat_id, prd_text)
        if prior is not None:
            _stream_update(api, chat_id, prd_text, Path(prior))
        else:
            _stream_build(api, chat_id, prd_text)


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
        card = _help_reply(text)
        if card:
            feishu_app.send_text(api, msg.chat_id, card)
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
