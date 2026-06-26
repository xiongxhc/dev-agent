"""Run report — ONE self-contained HTML deck per run (no external/CDN assets).

A PPT-style slide deck (one page at a time, light fade-up animation, ← → / click to advance) that
merges **this run** (rendered from the append-only ledger — pipeline, token/cost totals, acceptance,
preview URL) with a **how dev-agent works** explainer (the pipeline, where each part runs — local
Docker vs the Anthropic API — verify-from-source, agent-decided persistence, the triggers). The
renderer is pure: it reads only the events it is passed; acceptance/preview_url are passed in.
Anthropic-styled (warm paper, clay accent, serif headings)."""

import html
from pathlib import Path

_STYLE = """
:root{
  --paper:#F0EEE6; --ink:#191814; --muted:#6f6c62; --faint:#a39f93; --line:#dad6c9;
  --card:#f6f3ec; --clay:#C9613B; --clay-soft:#f1e3d8; --ghost:rgba(25,24,20,.05);
  --green:#356b42; --green-soft:#eef4ee; --red:#b23a2e; --red-soft:#f6e9e6;
  --serif:"Tiempos Headline", ui-serif, Georgia, "Times New Roman", serif;
  --sans:ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --mono:ui-monospace, SFMono-Regular, Menlo, monospace;
}
*{ box-sizing:border-box; }
html,body{ margin:0; height:100%; }
body{ font-family:var(--sans); background:var(--paper); color:var(--ink); line-height:1.5;
      overflow:hidden; -webkit-font-smoothing:antialiased; }
.slide{ position:fixed; inset:0; opacity:0; visibility:hidden; transform:translateY(16px);
        transition:opacity .55s ease, transform .55s ease; overflow:auto;
        display:grid; grid-template-rows:1fr auto; padding:6.5vh 8vw 0; }
.slide.active{ opacity:1; visibility:visible; transform:none; }
.inner{ align-self:center; max-width:64ch; width:100%; z-index:2; }
.ghost{ position:fixed; right:-1.5vw; top:50%; transform:translateY(-50%); font-family:var(--serif);
        font-weight:500; font-size:34vh; line-height:.8; color:var(--ghost); z-index:1;
        pointer-events:none; user-select:none; letter-spacing:-.04em; }
.rail{ position:fixed; left:8vw; top:6.5vh; bottom:12vh; width:0; border-left:2px solid var(--clay);
       opacity:.45; z-index:1; }
@keyframes rise{ from{opacity:0; transform:translateY(12px)} to{opacity:1; transform:none} }
.slide.active .inner>*{ animation:rise .5s ease both; }
.slide.active .inner>*:nth-child(2){ animation-delay:.06s; }
.slide.active .inner>*:nth-child(3){ animation-delay:.12s; }
.slide.active .inner>*:nth-child(4){ animation-delay:.18s; }
.slide.active .inner>*:nth-child(5){ animation-delay:.24s; }
@media (prefers-reduced-motion:reduce){ .slide{ transition:opacity .2s; } .slide.active .inner>*{ animation:none; } }

.eyebrow{ font-size:.74rem; letter-spacing:.18em; text-transform:uppercase; font-weight:600;
          color:var(--clay); margin:0 0 1rem; }
.eyebrow::before{ content:""; display:inline-block; width:1.6rem; height:2px; background:var(--clay);
          vertical-align:middle; margin-right:.6rem; transform:translateY(-.2em); }
h1{ font-family:var(--serif); font-weight:500; font-size:clamp(2rem,5vw,3.4rem); letter-spacing:-.02em;
    line-height:1.04; margin:0; }
h2{ font-family:var(--serif); font-weight:500; font-size:clamp(1.6rem,3.6vw,2.5rem); letter-spacing:-.01em;
    line-height:1.1; margin:0 0 1.1rem; max-width:18ch; }
.run-id{ font-family:var(--mono); color:var(--faint); font-size:.82rem; margin-top:1rem; }
.lead{ font-family:var(--serif); font-style:italic; font-size:clamp(1.05rem,2vw,1.4rem); color:var(--muted);
       max-width:30ch; margin:1.4rem 0 0; line-height:1.3; }
.sub{ font-size:clamp(.98rem,1.5vw,1.18rem); color:var(--muted); max-width:46ch; margin:0 0 1rem; }
.badge{ display:inline-flex; align-items:center; gap:.45rem; font-size:.82rem; font-weight:700;
        letter-spacing:.06em; padding:.4rem .95rem; border-radius:999px; border:1px solid transparent;
        margin-top:1.4rem; }
.badge::before{ content:""; width:.5rem; height:.5rem; border-radius:50%; background:currentColor; }
.badge.pass{ color:var(--green); background:var(--green-soft); border-color:#bcd3bf; }
.badge.fail{ color:var(--red); background:var(--red-soft); border-color:#e6c4bd; }
table{ width:100%; border-collapse:collapse; font-size:.88rem; }
thead th{ text-align:left; padding:.45rem .65rem; color:var(--muted); font-weight:600; font-size:.7rem;
          text-transform:uppercase; letter-spacing:.05em; border-bottom:1px solid var(--line); }
tbody td{ padding:.5rem .65rem; border-bottom:1px solid var(--line); vertical-align:top; }
tbody tr:last-child td{ border-bottom:0; }
td.num,th.num{ font-variant-numeric:tabular-nums; text-align:right; font-family:var(--mono); }
td.phase{ font-weight:600; }
.ok{ color:var(--green); font-weight:650; } .bad{ color:var(--red); font-weight:650; }
.reason{ color:var(--muted); font-size:.82rem; }
.totals{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:.8rem; margin-top:1.1rem; }
.card{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.8rem 1.05rem; }
.card .k{ font-family:var(--mono); font-size:.7rem; color:var(--clay); letter-spacing:.03em; }
.card .v{ font-size:1.3rem; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em; margin-top:.15rem; }
.preview{ background:var(--clay-soft); border:1px solid #e3c6b4; border-radius:12px; padding:.9rem 1.15rem;
          margin:0 0 1rem; display:flex; align-items:center; gap:.7rem; font-size:.95rem; flex-wrap:wrap; }
.preview .lbl{ font-family:var(--mono); color:var(--clay); font-weight:700; text-transform:uppercase;
          font-size:.7rem; letter-spacing:.06em; }
.preview a{ color:var(--clay); font-weight:600; word-break:break-all; }
.preview .host{ color:var(--muted); font-size:.8rem; }
.flow{ font-family:var(--mono); font-size:clamp(.58rem,.95vw,.8rem); line-height:1.65; white-space:pre;
       overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:12px;
       padding:1.05rem 1.25rem; box-shadow:0 16px 44px -30px rgba(25,24,20,.5); }
.flow .c{ color:var(--clay); } .flow .g{ color:var(--faint); }
.split{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin-top:.3rem; }
.col{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:1rem 1.2rem; }
.col.api{ background:#efe7dd; border-color:#e3d3c2; }
.col .h{ font-family:var(--mono); font-size:.7rem; letter-spacing:.04em; color:var(--clay);
         text-transform:uppercase; margin-bottom:.5rem; }
.col ul{ list-style:none; margin:0; padding:0; }
.col li{ font-size:.88rem; padding:.26rem 0 .26rem 1rem; position:relative; }
.col li::before{ content:"–"; position:absolute; left:0; color:var(--faint); }
.oneline{ font-family:var(--serif); font-size:clamp(1rem,1.7vw,1.2rem); margin:1rem 0 0; }
.points{ list-style:none; padding:0; margin:.3rem 0 0; max-width:58ch; }
.points li{ font-size:clamp(.94rem,1.4vw,1.1rem); padding:.5rem 0 .5rem 1.5rem; position:relative;
            border-top:1px solid var(--line); }
.points li:first-child{ border-top:0; }
.points li::before{ content:""; position:absolute; left:0; top:.95rem; width:.45rem; height:.45rem;
            border-radius:50%; background:var(--clay); }
code{ font-family:var(--mono); background:#ece3d7; padding:.06em .38em; border-radius:5px; font-size:.85em; }
.foot{ display:flex; justify-content:space-between; align-items:center; padding:1.1rem 0 1.5rem;
       border-top:1px solid var(--line); font-family:var(--mono); font-size:.72rem; color:var(--faint);
       letter-spacing:.04em; z-index:2; }
.foot .t{ color:var(--muted); }
.nav{ position:fixed; left:50%; bottom:1.5vh; transform:translateX(-50%); display:flex; gap:.4rem;
      z-index:6; }
.dot{ width:7px; height:7px; border-radius:50%; background:var(--line); border:0; padding:0; cursor:pointer;
      transition:background .25s, transform .25s; }
.dot.on{ background:var(--clay); transform:scale(1.3); }
.hint{ position:fixed; right:1rem; bottom:1.1rem; font-family:var(--mono); font-size:.68rem;
       color:var(--faint); z-index:6; }
@media (max-width:640px){ .split{ grid-template-columns:1fr; } .ghost{ display:none; } }
"""

_HOW_SLIDES = [
    ("How dev-agent works", """
    <h2>You describe it. It builds it.</h2>
    <p class='lead'>LLM brain, deterministic hands — the model decides what to build; plain code owns the run.</p>
    <div class='flow'><span class='c'>PRD</span> ─▶ scope ─▶ plan ─▶ build ─▶ verify ─▶ deploy ─▶ <span class='c'>preview + report</span>
   <span class='g'>└── a deterministic gate after every phase; fail one, the run stops ──┘</span></div>
    """),
    ("Where it runs", """
    <h2>You host the agent. Anthropic hosts the brain.</h2>
    <div class='split'>
      <div class='col'><div class='h'>Local · Docker on your machine</div>
        <ul><li>the Claude <b>Agent SDK</b> runtime</li><li>writing files, <code>pnpm</code> / <code>tsc</code> builds</li>
            <li>verify: rebuild from source + boot the app</li><li>deploy: the preview container(s)</li>
            <li>the Feishu bot + the pipeline</li></ul></div>
      <div class='col api'><div class='h'>Remote · the Anthropic API</div>
        <ul><li>the Claude <b>model</b> — all inference</li><li>reached with your API key</li>
            <li>the per-build cost (~$0.50)</li><li>the only thing that leaves the box,</li>
            <li>via an egress allowlist (api + npm)</li></ul></div>
    </div>
    <p class='oneline'>The agent and the build run on your hardware; the thinking happens on Anthropic's servers.</p>
    """),
    ("Why you can trust it", """
    <h2>Verify doesn't trust the build.</h2>
    <p class='sub'>A working build is a claim. Verify re-earns it from scratch, every time.</p>
    <ul class='points'>
      <li><b>Rebuild from source</b> in a clean container — a stale or hand-faked bundle can't pass.</li>
      <li><b>Boot the real app</b> and exercise it — status codes, JSON bodies, rendered selectors.</li>
      <li><b>Agent-decided persistence:</b> the scope model picks the store (none / SQLite / Postgres / Mongo);
          durability is proven by restarting the app while the datastore stays up and reading state back.</li>
    </ul>
    """),
    ("Two ways in", """
    <h2>Trigger from the CLI, or from Feishu.</h2>
    <p class='sub'><code>devagent run --build &lt;prd&gt;</code> is the primitive. Or drop a PRD into a Feishu
       chat: a bot runs this exact pipeline and <b>streams progress back, live</b> — scope ✓, plan ✓,
       build ✓, the preview URL — and relays clarifying questions if the request is ambiguous.</p>
    <p class='oneline'>Same pipeline underneath — the channel only changes how it's triggered.</p>
    """),
]


def _badge(status: str) -> str:
    if status == "succeeded":
        return '<span class="badge pass">PASS</span>'
    label = "FAIL" if status == "failed" else html.escape(status.upper())
    return f'<span class="badge fail">{label}</span>'


def _yn(ok: bool) -> str:
    return '<span class="ok">ok</span>' if ok else '<span class="bad">fail</span>'


def render_report(events: list[dict], run_id: str, *, preview_url: str | None = None,
                  acceptance: list[dict] | None = None) -> str:
    """Render the run's ledger + how-it-works explainer into one self-contained slide deck (HTML)."""
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
    for ph in by_event.get("phase", []):
        meta = ph.get("meta") or {}
        tin, tout = meta.get("tokens_in", 0), meta.get("tokens_out", 0)
        tot_in += tin
        tot_out += tout
        cost = meta.get("cost_usd")
        if cost is not None:
            tot_cost += cost
        if "repairs" in meta:
            total_repairs = (total_repairs or 0) + meta["repairs"]
        gate = gate_by_phase.get(ph.get("phase"))
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
            f"<td class='phase'>{html.escape(str(ph.get('phase', '')))}</td>"
            f"<td class='num'>{html.escape(str(ph.get('exit', '')))}</td>"
            f"<td>{gate_cell}</td>"
            f"<td class='num'>{tin:,}</td>"
            f"<td class='num'>{tout:,}</td>"
            f"<td class='num'>{cost_cell}</td>"
            "</tr>"
        )

    # ---- build the slides -------------------------------------------------
    slides: list[str] = []

    # Slide 1 — cover (this run)
    cover = [f"<p class='eyebrow'>dev-agent run report</p>",
             "<h1>Built, verified,<br>and deployed.</h1>",
             _badge(status),
             f"<div class='run-id'>{html.escape(run_id)}</div>"]
    if detail:
        cover.append(f"<p class='sub' style='margin-top:1rem'>{html.escape(detail)}</p>")
    slides.append("".join(cover))

    # Slide 2 — this run: pipeline + totals (+ preview if present)
    run_slide = ["<p class='eyebrow'>This run</p>", "<h2>The pipeline, phase by phase.</h2>"]
    if preview_url:
        href = html.escape(preview_url, quote=True)
        run_slide.append(f"<div class='preview'><span class='lbl'>Preview</span>"
                         f"<a href='{href}'>{html.escape(preview_url)}</a>"
                         "<span class='host'>opens on the host machine</span></div>")
    run_slide.append("<table><thead><tr><th>phase</th><th class='num'>exit</th><th>gate</th>"
                     "<th class='num'>tok in</th><th class='num'>tok out</th><th class='num'>cost</th>"
                     "</tr></thead><tbody>")
    run_slide.extend(rows)
    run_slide.append("</tbody></table>")
    run_slide.append("<div class='totals'>")
    run_slide.append(f"<div class='card'><div class='k'>tokens in</div><div class='v'>{tot_in:,}</div></div>")
    run_slide.append(f"<div class='card'><div class='k'>tokens out</div><div class='v'>{tot_out:,}</div></div>")
    run_slide.append(f"<div class='card'><div class='k'>cost</div><div class='v'>${tot_cost:.4f}</div></div>")
    if total_repairs is not None:
        run_slide.append(f"<div class='card'><div class='k'>repairs</div><div class='v'>{total_repairs}</div></div>")
    run_slide.append("</div>")
    egress = by_event.get("egress")
    if egress:
        net = html.escape(str(egress[-1].get("network", "")))
        run_slide.append(f"<p class='reason' style='margin-top:1rem'>Build ran egress-contained on "
                         f"<b>{net}</b> — only the Anthropic API and npm were reachable.</p>")
    slides.append("".join(run_slide))

    # Slide 3 — acceptance (only if present)
    if acceptance:
        acc = ["<p class='eyebrow'>Acceptance checks</p>", "<h2>Every criterion, machine-checked.</h2>",
               "<table><thead><tr><th>kind</th><th>route</th><th>result</th><th>detail</th>"
               "</tr></thead><tbody>"]
        for c in acceptance:
            acc.append(
                "<tr>"
                f"<td>{html.escape(str(c.get('kind', '')))}</td>"
                f"<td>{html.escape(str(c.get('route', '')))}</td>"
                f"<td>{_yn(c.get('ok', False))}</td>"
                f"<td class='reason'>{html.escape(str(c.get('detail', '')))}</td>"
                "</tr>"
            )
        acc.append("</tbody></table>")
        slides.append("".join(acc))

    # Slides 4+ — how it works
    for eyebrow, body in _HOW_SLIDES:
        slides.append(f"<p class='eyebrow'>{eyebrow}</p>{body}")

    total = len(slides)
    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append(f"<title>dev-agent · {html.escape(run_id)}</title>")
    parts.append(f"<style>{_STYLE}</style></head><body>")
    parts.append("<span class='rail'></span><span class='ghost' id='ghost'>0</span>")
    for i, body in enumerate(slides):
        active = " active" if i == 0 else ""
        running = "dev-agent" if i == 0 else html.escape(run_id)
        parts.append(
            f"<section class='slide{active}'><div class='inner'>{body}</div>"
            f"<div class='foot'><span class='t'>dev-agent run report</span>"
            f"<span>{i + 1:02d} / {total:02d}</span></div></section>"
        )
    dots = "".join(f"<button class='dot{(' on' if i == 0 else '')}' data-i='{i}'></button>"
                   for i in range(total))
    parts.append(f"<div class='nav'>{dots}</div><div class='hint'>← → · space · click</div>")
    parts.append(_DECK_JS)
    parts.append("</body></html>")
    return "".join(parts)


_DECK_JS = """<script>
(function(){
  var slides=[].slice.call(document.querySelectorAll('.slide'));
  var dots=[].slice.call(document.querySelectorAll('.dot'));
  var ghost=document.getElementById('ghost'); var i=0;
  function show(n){ n=Math.max(0,Math.min(slides.length-1,n));
    slides[i].classList.remove('active'); dots[i]&&dots[i].classList.remove('on');
    i=n; slides[i].classList.add('active'); dots[i]&&dots[i].classList.add('on');
    if(ghost) ghost.textContent=String(n); }
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
    """Write render_report(...) to run_dir/report.html and return the path."""
    path = Path(run_dir) / "report.html"
    path.write_text(render_report(events, run_id, preview_url=preview_url,
                                  acceptance=acceptance), encoding="utf-8")
    return path
