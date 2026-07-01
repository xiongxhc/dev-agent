# devagent/eval/report.py
"""Render an EvalResult to JSON (machine) + a self-contained HTML table (human). Pure: takes the
result, returns strings / writes files. The JSON is the durable A/B record; the HTML is a glance."""

import dataclasses
import html
import json
from pathlib import Path


def to_dict(result) -> dict:
    return dataclasses.asdict(result)


def _fmt(v, suffix="", nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def render_html(result) -> str:
    rows = []
    for fx in result.fixtures:
        if fx.scope_error:
            rows.append(f"<tr><td>{html.escape(fx.fixture)}</td>"
                        f"<td colspan='7' class='err'>scope/plan failed: {html.escape(fx.scope_error)}</td></tr>")
            continue
        for arm in fx.arms:
            if arm.unavailable:
                cells = f"<td colspan='6' class='na'>arm unavailable in this environment</td>"
            else:
                cells = (
                    f"<td>{_fmt(arm.acceptance_pass_rate * 100, '%', 0)}</td>"
                    f"<td>{_fmt(arm.mean_judge_overall, '', 1)}</td>"
                    f"<td>${_fmt(arm.mean_cost_token_usd, '', 4)}</td>"
                    f"<td>${_fmt(arm.mean_cost_all_in_usd, '', 4)}</td>"
                    f"<td>{_fmt(arm.mean_wall_clock_s, 's', 1)}</td>"
                    f"<td>{arm.runs}</td>"
                )
            rows.append(f"<tr><td>{html.escape(fx.title)}</td><td class='arm'>{html.escape(arm.arm)}</td>{cells}</tr>")
    body = "\n".join(rows) or "<tr><td colspan='8'>no fixtures</td></tr>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>dev-agent eval {html.escape(result.eval_id)}</title>
<style>
 body{{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#1a1a1a;background:#fafaf8}}
 h1{{font-size:1.1rem;letter-spacing:.02em}} .sub{{color:#777;font-size:.85rem;margin-bottom:1.2rem}}
 table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 th,td{{padding:.5rem .7rem;text-align:right;border-bottom:1px solid #eee}}
 th:first-child,td:first-child,td.arm{{text-align:left}}
 th{{background:#f3f2ee;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#555}}
 td.arm{{color:#666;font-variant:small-caps}} .err,.na{{color:#a33;text-align:left}}
</style></head><body>
<h1>dev-agent eval — {html.escape(result.eval_id)}</h1>
<div class="sub">A/B build-arm comparison. Acceptance is authoritative (rebuild→boot→checks);
judge is blinded &amp; indicative. Cost shown both as model-token and all-in (incl. session-hr).</div>
<table><thead><tr>
 <th>Fixture</th><th>Arm</th><th>Acceptance</th><th>Judge</th>
 <th>Token $</th><th>All-in $</th><th>Wall</th><th>Runs</th>
</tr></thead><tbody>
{body}
</tbody></table></body></html>"""


def write_report(eval_dir, result) -> tuple[Path, Path]:
    """Write eval-report.json + eval-report.html into *eval_dir*. Returns (json_path, html_path)."""
    d = Path(eval_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp, hp = d / "eval-report.json", d / "eval-report.html"
    jp.write_text(json.dumps(to_dict(result), indent=2))
    hp.write_text(render_html(result))
    return jp, hp
