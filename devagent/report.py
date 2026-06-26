"""Run report — ONE self-contained HTML deck per run (no external/CDN assets).

A build-console slide deck (one page at a time, ← → / click / dots, light fade) that merges
**this run** (rendered from the append-only ledger) with a **how dev-agent works** explainer.
Design language is the subject's own: a dark instrument panel, monospace-forward, with the
pipeline `scope → plan → build → verify → deploy` as the signature spine and build-status colors
(pass / running / fail) used functionally. The renderer is pure — it reads only the events passed
in; acceptance/preview_url are passed, not read from disk."""

import html
from pathlib import Path

_PIPELINE = ["scope", "plan", "build", "verify", "deploy"]

_STYLE = """
:root{
  --paper:#F0EEE6; --panel:#F8F5EE; --panel2:#EFEBE0; --line:#dcd8cc; --line2:#cfc9ba;
  --ink:#191814; --dim:#6f6c62; --faint:#a39f93;
  --accent:#C9613B; --pass:#3f7d4e; --run:#b07a2c; --fail:#b23a2e;
  --serif:"Tiempos Headline", ui-serif, Georgia, "Times New Roman", serif;
  --mono:ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans:ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
*{ box-sizing:border-box; }
html,body{ margin:0; height:100%; }
body{ background:
        radial-gradient(1100px 640px at 82% -8%, #f6f2e8 0%, rgba(246,242,232,0) 60%),
        var(--paper);
      color:var(--ink); font-family:var(--sans); line-height:1.5; overflow:hidden;
      -webkit-font-smoothing:antialiased; }
.slide{ position:fixed; inset:0; opacity:0; visibility:hidden; transform:translateY(14px);
        transition:opacity .5s ease, transform .5s ease; overflow:auto;
        display:flex; flex-direction:column; justify-content:center; padding:5vh 6vw; }
.slide.active{ opacity:1; visibility:visible; transform:none; }
.deck{ width:100%; max-width:1060px; margin:0 auto; }
@keyframes rise{ from{opacity:0; transform:translateY(10px)} to{opacity:1; transform:none} }
.slide.active .deck>*{ animation:rise .45s ease both; }
.slide.active .deck>*:nth-child(2){ animation-delay:.05s; }
.slide.active .deck>*:nth-child(3){ animation-delay:.1s; }
.slide.active .deck>*:nth-child(4){ animation-delay:.15s; }
.slide.active .deck>*:nth-child(5){ animation-delay:.2s; }
@media (prefers-reduced-motion:reduce){ .slide{ transition:opacity .2s; } .slide.active .deck>*{ animation:none; } }

/* top status strip — like a CI run header, on every slide */
.bar{ display:flex; align-items:center; gap:.7rem; font-family:var(--mono); font-size:.76rem;
      color:var(--dim); padding-bottom:.9rem; margin-bottom:2rem; border-bottom:1px solid var(--line);
      letter-spacing:.02em; }
.bar .logo{ color:var(--ink); font-weight:600; letter-spacing:.06em; }
.bar .logo b{ color:var(--accent); }
.bar .rid{ color:var(--faint); }
.bar .sp{ margin-left:auto; }
.pill{ display:inline-flex; align-items:center; gap:.4rem; font-family:var(--mono); font-size:.72rem;
       font-weight:600; letter-spacing:.08em; padding:.25rem .7rem; border-radius:999px;
       border:1px solid var(--line2); }
.pill::before{ content:""; width:.45rem; height:.45rem; border-radius:50%; background:currentColor; }
.pill.pass{ color:var(--pass); background:#eef4ee; border-color:#c4dcc6; }
.pill.fail{ color:var(--fail); background:#f7ebe9; border-color:#e2c2bc; }

.eyebrow{ font-family:var(--mono); font-size:.74rem; letter-spacing:.16em; text-transform:uppercase;
          color:var(--accent); margin:0 0 1rem; }
h1{ font-family:var(--serif); font-weight:500; font-size:clamp(2.1rem,4.8vw,3.5rem); letter-spacing:-.02em;
    line-height:1.04; margin:0; }
h2{ font-family:var(--serif); font-weight:500; font-size:clamp(1.6rem,3.2vw,2.4rem); letter-spacing:-.01em;
    line-height:1.1; margin:0 0 1.3rem; max-width:20ch; }
.sub{ font-size:clamp(1rem,1.5vw,1.18rem); color:var(--dim); max-width:54ch; margin:1.2rem 0 0; }
.kbd{ font-family:var(--mono); background:var(--panel2); border:1px solid var(--line); border-radius:5px;
      padding:.08em .45em; font-size:.85em; color:var(--ink); }

/* signature: the pipeline spine */
.pipe{ display:flex; align-items:stretch; gap:0; margin-top:2.2rem; flex-wrap:wrap; }
.node{ flex:1 1 0; min-width:120px; background:var(--panel); border:1px solid var(--line);
       border-radius:10px; padding:.9rem 1rem; position:relative; }
.node + .node{ margin-left:.6rem; }
.node .n{ font-family:var(--mono); font-size:.72rem; color:var(--faint); }
.node .ph{ font-family:var(--mono); font-size:1.02rem; font-weight:600; color:var(--ink);
           margin-top:.2rem; display:flex; align-items:center; gap:.5rem; }
.node .ph::before{ content:""; width:.5rem; height:.5rem; border-radius:50%; background:var(--line2); }
.node.ok .ph::before{ background:var(--pass); box-shadow:0 0 0 3px rgba(63,125,78,.14); }
.node.bad .ph::before{ background:var(--fail); box-shadow:0 0 0 3px rgba(178,58,46,.14); }
.node .meta{ font-family:var(--mono); font-size:.72rem; color:var(--dim); margin-top:.5rem; }
@media (max-width:720px){ .node{ flex:1 1 40%; } .node + .node{ margin-left:0; } .pipe{ gap:.5rem; } }

/* telemetry totals */
.tele{ display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:.7rem; margin-top:1.4rem; }
.metric{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:.85rem 1rem; }
.metric .k{ font-family:var(--mono); font-size:.7rem; color:var(--dim); letter-spacing:.04em; text-transform:uppercase; }
.metric .v{ font-family:var(--mono); font-size:1.4rem; font-weight:600; color:var(--ink); margin-top:.25rem;
            font-variant-numeric:tabular-nums; }

.panel{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
table{ width:100%; border-collapse:collapse; font-family:var(--mono); font-size:.85rem; }
thead th{ text-align:left; padding:.6rem .9rem; color:var(--dim); font-weight:500; font-size:.68rem;
          text-transform:uppercase; letter-spacing:.06em; background:var(--panel2);
          border-bottom:1px solid var(--line); }
tbody td{ padding:.55rem .9rem; border-bottom:1px solid var(--line); vertical-align:top; color:var(--ink); }
tbody tr:last-child td{ border-bottom:0; }
td.num,th.num{ text-align:right; font-variant-numeric:tabular-nums; }
.ok{ color:var(--pass); } .bad{ color:var(--fail); }
.reason{ color:var(--dim); font-size:.8rem; }

.line-item{ font-family:var(--mono); font-size:.82rem; color:var(--dim); margin-top:1.1rem; }
.line-item b{ color:var(--ink); }
.preview{ display:flex; align-items:center; gap:.7rem; flex-wrap:wrap; font-family:var(--mono);
          font-size:.85rem; background:var(--panel); border:1px solid var(--line2); border-left:3px solid var(--accent);
          border-radius:8px; padding:.7rem 1rem; margin-bottom:1.3rem; }
.preview .lbl{ color:var(--accent); font-size:.7rem; letter-spacing:.06em; text-transform:uppercase; }
.preview a{ color:var(--ink); word-break:break-all; }
.preview .host{ color:var(--faint); margin-left:auto; font-size:.74rem; }

.flow{ font-family:var(--mono); font-size:clamp(.6rem,.95vw,.82rem); line-height:1.7; white-space:pre;
       overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px;
       padding:1.1rem 1.3rem; color:var(--ink); }
.flow .c{ color:var(--accent); } .flow .g{ color:var(--faint); }
.split{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin-top:.3rem; }
.col{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:1.1rem 1.3rem; }
.col.api{ border-top:3px solid var(--accent); }
.col.local{ border-top:3px solid var(--pass); }
.col .h{ font-family:var(--mono); font-size:.7rem; letter-spacing:.05em; text-transform:uppercase;
         color:var(--dim); margin-bottom:.7rem; }
.col ul{ list-style:none; margin:0; padding:0; }
.col li{ font-size:.92rem; padding:.3rem 0 .3rem 1.1rem; position:relative; color:var(--ink); }
.col li::before{ content:"›"; position:absolute; left:0; color:var(--accent); }
.oneline{ font-family:var(--serif); font-size:clamp(1.05rem,1.7vw,1.3rem); color:var(--ink); margin:1.2rem 0 0; }
.oneline b{ color:var(--accent); font-weight:600; }
.points{ list-style:none; padding:0; margin:.4rem 0 0; }
.points li{ font-size:clamp(.95rem,1.4vw,1.1rem); padding:.6rem 0 .6rem 1.6rem; position:relative;
            border-top:1px solid var(--line); color:var(--ink); }
.points li:first-child{ border-top:0; }
.points li::before{ content:""; position:absolute; left:0; top:1.05rem; width:.4rem; height:.4rem;
            background:var(--accent); transform:rotate(45deg); }
.cards{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin-top:.3rem; }
.tile{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:1.2rem 1.3rem; }
.tile .k{ font-family:var(--mono); font-size:.72rem; color:var(--accent); letter-spacing:.04em; }
.tile .t{ font-family:var(--mono); font-size:1.05rem; font-weight:600; margin:.35rem 0 .3rem; }
.tile .d{ font-size:.9rem; color:var(--dim); line-height:1.45; }

.foot{ display:flex; justify-content:space-between; align-items:center; margin-top:2rem;
       padding-top:1rem; border-top:1px solid var(--line); font-family:var(--mono); font-size:.7rem;
       color:var(--faint); }
.nav{ position:fixed; left:50%; bottom:2.2vh; transform:translateX(-50%); display:flex; gap:.45rem; z-index:6; }
.dot{ width:6px; height:6px; border-radius:50%; background:var(--line2); border:0; padding:0; cursor:pointer;
      transition:background .25s, transform .25s; }
.dot:focus-visible{ outline:2px solid var(--accent); outline-offset:3px; }
.dot.on{ background:var(--accent); transform:scale(1.4); }
.hint{ position:fixed; right:1.1rem; bottom:1.7vh; font-family:var(--mono); font-size:.66rem;
       color:var(--faint); z-index:6; }
a{ color:var(--accent); }
@media (max-width:640px){ .split,.cards{ grid-template-columns:1fr; } }
"""


def _badge(status: str) -> str:
    if status == "succeeded":
        return '<span class="pill pass">PASS</span>'
    label = "FAIL" if status == "failed" else html.escape(status.upper())
    return f'<span class="pill fail">{label}</span>'


def _yn(ok: bool) -> str:
    return '<span class="ok">ok</span>' if ok else '<span class="bad">fail</span>'


def _spine(phase_status: dict[str, bool | None]) -> str:
    """The pipeline signature: 01..05 phase nodes, gate status as a colored dot."""
    out = ['<div class="pipe">']
    for i, ph in enumerate(_PIPELINE, 1):
        ok = phase_status.get(ph)
        cls = "node ok" if ok is True else ("node bad" if ok is False else "node")
        out.append(f'<div class="{cls}"><div class="n">{i:02d}</div><div class="ph">{ph}</div></div>')
    out.append("</div>")
    return "".join(out)


def render_report(events: list[dict], run_id: str, *, preview_url: str | None = None,
                  acceptance: list[dict] | None = None) -> str:
    by_event: dict[str, list[dict]] = {}
    for e in events:
        by_event.setdefault(e.get("event", ""), []).append(e)

    run_end = (by_event.get("run_end") or [{}])[-1]
    status = run_end.get("status", "unknown")
    detail = run_end.get("detail", "")
    gate_by_phase = {g["phase"]: g for g in by_event.get("gate", [])}

    rows = []
    tot_in = tot_out = 0
    tot_cost = 0.0
    total_repairs = None
    phase_status: dict[str, bool | None] = {}
    for ph in by_event.get("phase", []):
        name = ph.get("phase")
        meta = ph.get("meta") or {}
        tin, tout = meta.get("tokens_in", 0), meta.get("tokens_out", 0)
        tot_in += tin
        tot_out += tout
        cost = meta.get("cost_usd")
        if cost is not None:
            tot_cost += cost
        if "repairs" in meta:
            total_repairs = (total_repairs or 0) + meta["repairs"]
        gate = gate_by_phase.get(name)
        if name in _PIPELINE:
            phase_status[name] = None if gate is None else bool(gate.get("ok"))
        if gate is None:
            gate_cell = '<span class="reason">—</span>'
        else:
            reason = gate.get("reason") or ""
            gate_cell = _yn(gate.get("ok", False))
            if reason:
                gate_cell += f' <span class="reason">{html.escape(reason)}</span>'
        cost_cell = f"${cost:.4f}" if cost is not None else "—"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(name or ''))}</td>"
            f"<td class='num'>{html.escape(str(ph.get('exit', '')))}</td>"
            f"<td>{gate_cell}</td>"
            f"<td class='num'>{tin:,}</td>"
            f"<td class='num'>{tout:,}</td>"
            f"<td class='num'>{cost_cell}</td>"
            "</tr>"
        )

    cost_str = f"${tot_cost:.4f}"

    def bar(extra: str = "") -> str:
        return (f"<div class='bar'><span class='logo'><b>◢</b> dev-agent</span>"
                f"<span class='rid'>{html.escape(run_id)}</span>"
                f"<span class='sp'></span>{extra}{_badge(status)}</div>")

    slides: list[str] = []

    # 1 — cover: headline + the pipeline spine (signature)
    cover = [bar(), "<p class='eyebrow'>run report</p>",
             "<h1>An autonomous build,<br>start to finish.</h1>",
             _spine(phase_status)]
    if detail:
        cover.append(f"<p class='sub'>{html.escape(detail)}</p>")
    else:
        cover.append("<p class='sub'>Scope → plan → build → verify → deploy — a deterministic gate "
                     "after every phase. Here's what this run did, and how dev-agent did it.</p>")
    slides.append("".join(cover))

    # 2 — this run: telemetry + the phase table
    run_slide = [bar(f"<span class='rid'>{cost_str}</span>&nbsp;&nbsp;"),
                 "<p class='eyebrow'>this run</p>", "<h2>Phase by phase.</h2>"]
    if preview_url:
        href = html.escape(preview_url, quote=True)
        run_slide.append(f"<div class='preview'><span class='lbl'>Preview</span>"
                         f"<a href='{href}'>{html.escape(preview_url)}</a>"
                         "<span class='host'>opens on the host machine</span></div>")
    run_slide.append("<div class='panel'><table><thead><tr><th>phase</th><th class='num'>exit</th>"
                     "<th>gate</th><th class='num'>tok in</th><th class='num'>tok out</th>"
                     "<th class='num'>cost</th></tr></thead><tbody>")
    run_slide.extend(rows)
    run_slide.append("</tbody></table></div>")
    run_slide.append("<div class='tele'>")
    run_slide.append(f"<div class='metric'><div class='k'>tokens in</div><div class='v'>{tot_in:,}</div></div>")
    run_slide.append(f"<div class='metric'><div class='k'>tokens out</div><div class='v'>{tot_out:,}</div></div>")
    run_slide.append(f"<div class='metric'><div class='k'>cost</div><div class='v'>{cost_str}</div></div>")
    if total_repairs is not None:
        run_slide.append(f"<div class='metric'><div class='k'>repairs</div><div class='v'>{total_repairs}</div></div>")
    run_slide.append("</div>")
    egress = by_event.get("egress")
    if egress:
        net = html.escape(str(egress[-1].get("network", "")))
        run_slide.append(f"<p class='line-item'>build ran egress-contained on <b>{net}</b> — only the "
                         "Anthropic API and npm were reachable.</p>")
    slides.append("".join(run_slide))

    # 3 — acceptance (only if present)
    if acceptance:
        acc = [bar(), "<p class='eyebrow'>acceptance</p>", "<h2>Every criterion, machine-checked.</h2>",
               "<div class='panel'><table><thead><tr><th>kind</th><th>route</th><th>result</th>"
               "<th>detail</th></tr></thead><tbody>"]
        for c in acceptance:
            acc.append(
                "<tr>"
                f"<td>{html.escape(str(c.get('kind', '')))}</td>"
                f"<td>{html.escape(str(c.get('route', '')))}</td>"
                f"<td>{_yn(c.get('ok', False))}</td>"
                f"<td class='reason'>{html.escape(str(c.get('detail', '')))}</td>"
                "</tr>"
            )
        acc.append("</tbody></table></div>")
        slides.append("".join(acc))

    # 4 — how it works
    slides.append(bar() + "<p class='eyebrow'>how it works</p>"
        "<h2>LLM brain, deterministic hands.</h2>"
        "<p class='sub'>The model decides <i>what</i> to build. Plain code owns <i>how the run "
        "proceeds</i> — sequencing, budgets, and a hard gate after every phase. The build's own "
        "'it worked' is a claim, not a result.</p>"
        "<div class='flow' style='margin-top:1.6rem'><span class='c'>PRD</span> ─▶ scope ─▶ plan ─▶ build ─▶ verify ─▶ deploy ─▶ <span class='c'>preview + report</span>\n"
        "   <span class='g'>└── fail any gate, the run stops and says why ──┘</span></div>")

    # 5 — where it runs (the local/remote split)
    slides.append(bar() + "<p class='eyebrow'>where it runs</p>"
        "<h2>You host the agent. Anthropic hosts the brain.</h2>"
        "<div class='split'>"
        "<div class='col local'><div class='h'>local · docker on your machine</div><ul>"
        "<li>the Claude <b>Agent SDK</b> runtime</li><li>writing files, <span class='kbd'>pnpm</span> / <span class='kbd'>tsc</span> builds</li>"
        "<li>verify: rebuild from source + boot the app</li><li>deploy: the preview container(s)</li>"
        "<li>the Feishu bot + the pipeline</li></ul></div>"
        "<div class='col api'><div class='h'>remote · the anthropic api</div><ul>"
        "<li>the Claude <b>model</b> — all inference</li><li>reached with your API key</li>"
        "<li>the per-build cost (~$0.50)</li><li>the only thing that leaves the box,</li>"
        "<li>via an egress allowlist (api + npm)</li></ul></div></div>"
        "<p class='oneline'>The agent and the build run on <b>your hardware</b>; the thinking happens on "
        "<b>Anthropic's servers</b>.</p>")

    # 6 — why you can trust it
    slides.append(bar() + "<p class='eyebrow'>why you can trust it</p>"
        "<h2>Verify doesn't trust the build.</h2>"
        "<ul class='points'>"
        "<li><b>Rebuild from source</b> in a clean container — a stale or hand-faked bundle can't pass.</li>"
        "<li><b>Boot the real app</b> and exercise it — status codes, JSON bodies, rendered selectors.</li>"
        "<li><b>Agent-decided persistence:</b> the scope model picks the store (none / SQLite / Postgres / "
        "Mongo); durability is proven by restarting the app while the datastore stays up and reading state back.</li>"
        "</ul>")

    # 7 — two ways in
    slides.append(bar() + "<p class='eyebrow'>two ways in</p>"
        "<h2>Trigger from the CLI, or from Feishu.</h2>"
        "<div class='cards'>"
        "<div class='tile'><div class='k'>cli</div><div class='t'>devagent run --build</div>"
        "<div class='d'>The primitive everything wraps. One command, a PRD file, a built app.</div></div>"
        "<div class='tile'><div class='k'>feishu</div><div class='t'>drop a PRD in chat</div>"
        "<div class='d'>A bot runs this exact pipeline and streams progress back into the chat, live.</div></div>"
        "</div><p class='oneline'>Same pipeline underneath — the channel only changes how it's triggered.</p>")

    total = len(slides)
    out: list[str] = []
    out.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    out.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    out.append(f"<title>dev-agent · {html.escape(run_id)}</title>")
    out.append(f"<style>{_STYLE}</style></head><body>")
    for i, body in enumerate(slides):
        active = " active" if i == 0 else ""
        out.append(f"<section class='slide{active}'><div class='deck'>{body}"
                   f"<div class='foot'><span>dev-agent run report</span>"
                   f"<span>{i + 1:02d} / {total:02d}</span></div></div></section>")
    dots = "".join(f"<button class='dot{(' on' if i == 0 else '')}' data-i='{i}' "
                   f"aria-label='slide {i + 1}'></button>" for i in range(total))
    out.append(f"<div class='nav'>{dots}</div><div class='hint'>← → · space · click</div>")
    out.append(_DECK_JS)
    out.append("</body></html>")
    return "".join(out)


_DECK_JS = """<script>
(function(){
  var slides=[].slice.call(document.querySelectorAll('.slide'));
  var dots=[].slice.call(document.querySelectorAll('.dot')); var i=0;
  function show(n){ n=Math.max(0,Math.min(slides.length-1,n));
    slides[i].classList.remove('active'); dots[i]&&dots[i].classList.remove('on');
    i=n; slides[i].classList.add('active'); dots[i]&&dots[i].classList.add('on'); }
  addEventListener('keydown',function(e){
    if(['ArrowRight','ArrowDown',' ','PageDown'].indexOf(e.key)>-1){ e.preventDefault(); show(i+1); }
    if(['ArrowLeft','ArrowUp','PageUp'].indexOf(e.key)>-1){ e.preventDefault(); show(i-1); } });
  addEventListener('click',function(e){ if(e.target.closest('a,.dot')) return; show(i+1); });
  dots.forEach(function(d){ d.addEventListener('click',function(ev){ ev.stopPropagation();
    show(+d.dataset.i); }); });
})();
</script>"""


def write_report(run_dir, events: list[dict], run_id: str, *,
                 preview_url: str | None = None, acceptance: list[dict] | None = None) -> Path:
    path = Path(run_dir) / "report.html"
    path.write_text(render_report(events, run_id, preview_url=preview_url,
                                  acceptance=acceptance), encoding="utf-8")
    return path
