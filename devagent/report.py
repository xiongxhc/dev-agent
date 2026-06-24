"""M3 run report — a self-contained HTML summary of a run, rendered from the ledger.

A run leaves an append-only ledger.jsonl; this turns that audit trail into one offline
HTML page (inline CSS, no external/CDN assets) summarizing what happened: the pipeline
table, token/cost totals, acceptance checks, and the preview URL. The renderer is pure —
it reads only the events list passed to it; acceptance (and preview_url) are passed in by
the caller rather than read from disk, so the report has no side dependencies."""

import html
from pathlib import Path

_STYLE = """
:root { --green: #137333; --red: #c5221f; --bg: #f6f8fa; --line: #d0d7de; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
       sans-serif; margin: 0; padding: 2rem; color: #1f2328; background: #fff; line-height: 1.5; }
.wrap { max-width: 880px; margin: 0 auto; }
header { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
         border-bottom: 1px solid var(--line); padding-bottom: 1rem; margin-bottom: 1.5rem; }
h1 { font-size: 1.25rem; margin: 0; font-weight: 600; }
.run-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #57606a; }
.badge { font-size: 1rem; font-weight: 700; letter-spacing: .05em; color: #fff;
         padding: .35rem .9rem; border-radius: 6px; margin-left: auto; }
.badge.pass { background: var(--green); }
.badge.fail { background: var(--red); }
h2 { font-size: .95rem; text-transform: uppercase; letter-spacing: .04em; color: #57606a;
     margin: 1.75rem 0 .6rem; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { background: var(--bg); font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; text-align: right;
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ok { color: var(--green); font-weight: 600; }
.bad { color: var(--red); font-weight: 600; }
.reason { color: #57606a; font-size: .85rem; }
.totals { display: flex; gap: 2rem; flex-wrap: wrap; background: var(--bg);
          border: 1px solid var(--line); border-radius: 8px; padding: 1rem 1.25rem; }
.totals .k { font-size: .8rem; color: #57606a; text-transform: uppercase; letter-spacing: .03em; }
.totals .v { font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.preview { background: #ddf4ff; border: 1px solid #54aeff; border-radius: 8px;
           padding: .9rem 1.1rem; margin: 1.5rem 0; font-size: 1rem; }
.preview a { color: #0969da; font-weight: 600; word-break: break-all; }
.note { color: #57606a; font-size: .85rem; margin-top: 1.5rem; }
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
            f"<td>{html.escape(str(ph.get('phase', '')))}</td>"
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
    parts.append(f"<div><h1>dev-agent run report</h1>"
                 f"<div class='run-id'>{html.escape(run_id)}</div></div>")
    parts.append(_badge(status))
    parts.append("</header>")

    if detail:
        parts.append(f"<p class='reason'>{html.escape(detail)}</p>")

    if preview_url:
        href = html.escape(preview_url, quote=True)
        parts.append(f"<div class='preview'>Preview: "
                     f"<a href='{href}'>{html.escape(preview_url)}</a></div>")

    parts.append("<h2>Pipeline</h2>")
    parts.append("<table><thead><tr><th>phase</th><th>exit</th><th>gate</th>"
                 "<th>tokens in</th><th>tokens out</th><th>cost</th></tr></thead><tbody>")
    parts.extend(rows)
    parts.append("</tbody></table>")

    parts.append("<h2>Totals</h2><div class='totals'>")
    parts.append(f"<div><div class='k'>tokens in</div><div class='v'>{tot_in:,}</div></div>")
    parts.append(f"<div><div class='k'>tokens out</div><div class='v'>{tot_out:,}</div></div>")
    parts.append(f"<div><div class='k'>cost</div><div class='v'>${tot_cost:.4f}</div></div>")
    if total_repairs is not None:
        parts.append(f"<div><div class='k'>repairs</div><div class='v'>{total_repairs}</div></div>")
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

    parts.append("</div></body></html>")
    return "".join(parts)


def write_report(run_dir, events: list[dict], run_id: str, *,
                 preview_url: str | None = None, acceptance: list[dict] | None = None) -> Path:
    """Write render_report(...) to run_dir/report.html and return the path."""
    path = Path(run_dir) / "report.html"
    path.write_text(render_report(events, run_id, preview_url=preview_url,
                                  acceptance=acceptance), encoding="utf-8")
    return path
