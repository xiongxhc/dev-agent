"""M3 run report — a self-contained HTML summary of a run, rendered from the ledger.

A run leaves an append-only ledger.jsonl; this turns that audit trail into one offline
HTML page (inline CSS, no external/CDN assets) summarizing what happened: the pipeline
table, token/cost totals, acceptance checks, and the preview URL. The renderer is pure —
it reads only the events list passed to it; acceptance (and preview_url) are passed in by
the caller rather than read from disk, so the report has no side dependencies."""

import html
from pathlib import Path

_STYLE = """
:root {
  --ink: #0f172a; --muted: #64748b; --faint: #94a3b8; --line: #e2e8f0;
  --panel: #f8fafc; --accent: #4f46e5; --accent-soft: #eef2ff;
  --green: #15803d; --green-soft: #f0fdf4; --red: #b91c1c; --red-soft: #fef2f2;
  --radius: 12px; --shadow: 0 1px 2px rgba(15,23,42,.04), 0 6px 24px -12px rgba(15,23,42,.12);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
       Helvetica, Arial, sans-serif; margin: 0; padding: 2.5rem 1.25rem; color: var(--ink);
       background: linear-gradient(180deg, #f1f5f9 0%, #f8fafc 240px, #fff 600px);
       line-height: 1.55; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 920px; margin: 0 auto; background: #fff; border: 1px solid var(--line);
        border-radius: 16px; box-shadow: var(--shadow); overflow: hidden; }
header { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
         padding: 1.6rem 2rem; border-bottom: 1px solid var(--line);
         background: linear-gradient(180deg, #fff, var(--panel)); }
.brand { display: flex; align-items: center; gap: .7rem; }
.mark { width: 34px; height: 34px; border-radius: 9px; flex: 0 0 auto;
        background: linear-gradient(135deg, var(--accent), #7c3aed); color: #fff;
        display: grid; place-items: center; font-weight: 800; font-size: 1rem;
        box-shadow: 0 2px 8px -2px rgba(79,70,229,.5); }
h1 { font-size: 1.15rem; margin: 0; font-weight: 650; letter-spacing: -.01em; }
.run-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--faint);
          font-size: .82rem; margin-top: .15rem; }
.badge { font-size: .8rem; font-weight: 700; letter-spacing: .06em; padding: .4rem .85rem;
         border-radius: 999px; margin-left: auto; display: inline-flex; align-items: center;
         gap: .4rem; border: 1px solid transparent; }
.badge::before { content: ""; width: .5rem; height: .5rem; border-radius: 50%; background: currentColor; }
.badge.pass { color: var(--green); background: var(--green-soft); border-color: #bbf7d0; }
.badge.fail { color: var(--red); background: var(--red-soft); border-color: #fecaca; }
.body { padding: 1.4rem 2rem 2rem; }
h2 { font-size: .76rem; text-transform: uppercase; letter-spacing: .08em; color: var(--faint);
     font-weight: 700; margin: 2rem 0 .75rem; }
.detail { color: var(--muted); font-size: .9rem; margin: .25rem 0 0; }
table { width: 100%; border-collapse: collapse; font-size: .88rem; }
thead th { text-align: left; padding: .5rem .7rem; color: var(--muted); font-weight: 600;
           font-size: .74rem; text-transform: uppercase; letter-spacing: .05em;
           border-bottom: 1px solid var(--line); }
tbody td { text-align: left; padding: .6rem .7rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--panel); }
td.num, th.num { font-variant-numeric: tabular-nums; text-align: right;
                 font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
td.phase { font-weight: 600; }
.ok { color: var(--green); font-weight: 650; }
.bad { color: var(--red); font-weight: 650; }
.reason { color: var(--muted); font-size: .82rem; }
.totals { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: .9rem; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
        padding: .9rem 1.1rem; }
.card .k { font-size: .72rem; color: var(--faint); text-transform: uppercase; letter-spacing: .05em;
           font-weight: 600; }
.card .v { font-size: 1.4rem; font-weight: 700; font-variant-numeric: tabular-nums;
           letter-spacing: -.02em; margin-top: .2rem; }
.preview { background: var(--accent-soft); border: 1px solid #c7d2fe; border-radius: var(--radius);
           padding: 1rem 1.2rem; margin: .25rem 0 .5rem; font-size: .95rem; display: flex;
           align-items: center; gap: .6rem; }
.preview .lbl { color: var(--accent); font-weight: 700; text-transform: uppercase;
                font-size: .72rem; letter-spacing: .06em; }
.preview a { color: var(--accent); font-weight: 600; word-break: break-all; text-decoration: none; }
.preview a:hover { text-decoration: underline; }
.note { color: var(--faint); font-size: .82rem; margin-top: 1.75rem; padding-top: 1rem;
        border-top: 1px solid var(--line); }
"""


def _badge(status: str) -> str:
    """A big PASS/FAIL badge from the run_end status (anything but 'succeeded' is FAIL)."""
    if status == "succeeded":
        return '<span class="badge pass">PASS</span>'
    label = "FAIL" if status == "failed" else html.escape(status.upper())
    return f'<span class="badge fail">{label}</span>'


def _yn(ok: bool) -> str:
    return '<span class="ok">ok</span>' if ok else '<span class="bad">fail</span>'


def render_report(events: list[dict], run_id: str, *, preview_url: str | None = None,
                  acceptance: list[dict] | None = None) -> str:
    """Render the run's ledger into a self-contained HTML document (no external assets)."""
    by_event: dict[str, list[dict]] = {}
    for e in events:
        by_event.setdefault(e.get("event", ""), []).append(e)

    run_end = (by_event.get("run_end") or [{}])[-1]
    status = run_end.get("status", "unknown")
    detail = run_end.get("detail", "")

    # gate keyed by phase (last one wins, matching the orchestrator's one-pass loop)
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

    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append(f"<title>dev-agent run {html.escape(run_id)}</title>")
    parts.append(f"<style>{_STYLE}</style></head><body><div class='wrap'>")

    parts.append("<header>")
    parts.append("<div class='brand'><div class='mark'>DA</div>"
                 "<div><h1>dev-agent run report</h1>"
                 f"<div class='run-id'>{html.escape(run_id)}</div></div></div>")
    parts.append(_badge(status))
    parts.append("</header>")
    parts.append("<div class='body'>")

    if detail:
        parts.append(f"<p class='detail'>{html.escape(detail)}</p>")

    if preview_url:
        href = html.escape(preview_url, quote=True)
        parts.append(f"<div class='preview'><span class='lbl'>Preview</span>"
                     f"<a href='{href}'>{html.escape(preview_url)}</a></div>")

    parts.append("<h2>Pipeline</h2>")
    parts.append("<table><thead><tr><th>phase</th><th>exit</th><th>gate</th>"
                 "<th>tokens in</th><th>tokens out</th><th>cost</th></tr></thead><tbody>")
    parts.extend(rows)
    parts.append("</tbody></table>")

    parts.append("<h2>Totals</h2><div class='totals'>")
    parts.append(f"<div class='card'><div class='k'>tokens in</div><div class='v'>{tot_in:,}</div></div>")
    parts.append(f"<div class='card'><div class='k'>tokens out</div><div class='v'>{tot_out:,}</div></div>")
    parts.append(f"<div class='card'><div class='k'>cost</div><div class='v'>${tot_cost:.4f}</div></div>")
    if total_repairs is not None:
        parts.append(f"<div class='card'><div class='k'>repairs</div><div class='v'>{total_repairs}</div></div>")
    parts.append("</div>")

    if acceptance:
        parts.append("<h2>Acceptance checks</h2>")
        parts.append("<table><thead><tr><th>kind</th><th>route</th><th>result</th>"
                     "<th>detail</th></tr></thead><tbody>")
        for c in acceptance:
            parts.append(
                "<tr>"
                f"<td>{html.escape(str(c.get('kind', '')))}</td>"
                f"<td>{html.escape(str(c.get('route', '')))}</td>"
                f"<td>{_yn(c.get('ok', False))}</td>"
                f"<td class='reason'>{html.escape(str(c.get('detail', '')))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    egress = by_event.get("egress")
    if egress:
        net = html.escape(str(egress[-1].get("network", "")))
        parts.append(f"<p class='note'>egress-contained: {net}</p>")

    parts.append("</div></div></body></html>")  # close .body, .wrap
    return "".join(parts)


def write_report(run_dir, events: list[dict], run_id: str, *,
                 preview_url: str | None = None, acceptance: list[dict] | None = None) -> Path:
    """Write render_report(...) to run_dir/report.html and return the path."""
    path = Path(run_dir) / "report.html"
    path.write_text(render_report(events, run_id, preview_url=preview_url,
                                  acceptance=acceptance), encoding="utf-8")
    return path
