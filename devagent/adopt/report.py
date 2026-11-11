"""The adopt report — what a human reads before any of this is treated as truth.

Adoption ends at a human review gate, so the report has one job: make what is NOT known as
visible as what is. An adoption whose report reads like a finished design invites exactly the
mistake the gate exists to prevent, so unmapped services, recipe gaps and failed rebuilds are
stated up front rather than buried under a service list.
"""

import html
from pathlib import Path

_CSS = """
body{font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#1d2129;
 max-width:60rem;margin:0 auto;padding:2rem 1.25rem}
h1{font-size:1.6rem;margin:0 0 .25rem}h2{font-size:1.05rem;margin:1.75rem 0 .5rem}
.sub{color:#6b7280;margin:0 0 1.5rem}
table{width:100%;border-collapse:collapse;margin:.75rem 0 1.5rem}
th,td{text-align:left;padding:.5rem;border-bottom:1px solid #e5e7eb;vertical-align:top}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:#6b7280}
code{background:#f3f4f6;padding:.1rem .3rem;border-radius:3px;font-size:.9em}
.proven{color:#067647;font-weight:600}.unproven{color:#b42318;font-weight:600}
.unmappable{color:#b54708;font-weight:600}
.gate{border:1px solid #f59e0b;background:#fffbeb;padding:.9rem 1rem;border-radius:6px;
 margin:1.5rem 0}
.gap{color:#b54708}.muted{color:#6b7280;font-size:.92rem}
"""


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _service_row(node, verdict, run_dir=None) -> str:
    status = verdict.status if verdict else ""
    detail = _esc((verdict.detail if verdict else "")[:170])
    # "build exited 1" is not something a reviewer can act on. When prove kept the log, point at
    # it — the row is where they are already looking.
    log = Path(run_dir) / "services" / node.name / "build.log" if run_dir else None
    if log is not None and log.is_file():
        detail += f" <a href='services/{_esc(node.name)}/build.log'>build log</a>"
    return (f"<tr><td><code>{_esc(node.id)}</code></td>"
            f"<td>{_esc(node.kind)}</td>"
            f"<td><code>{_esc(node.stack)}</code></td>"
            f"<td class='{_esc(status)}'>{_esc(status or '—')}</td>"
            f"<td class='muted'>{detail}</td></tr>")


def _caveats(result) -> list[str]:
    """What the reviewer must not miss. Ordered by how badly it would mislead if overlooked."""
    from ..contract_utils import derive_integration_checks

    survey, design, counts = result.survey, result.design, result.counts
    unmapped = [s.name for s in (design.services if design else [])
                if s.prd_slice.startswith("NOT MAPPED")]
    out = []
    if unmapped:
        out.append(f"<strong>{len(unmapped)} service(s) were not mapped</strong> — "
                   f"{_esc(', '.join(unmapped))}. Nothing in their description comes from "
                   f"reading the code; it is survey evidence only.")
    if survey.gaps:
        out.append(f"<strong>{len(survey.gaps)} recipe gap(s)</strong> — a service with no "
                   f"registered recipe was never built, so its description is unverified.")
    if counts.get("unproven"):
        out.append(f"<strong>{counts['unproven']} service(s) failed to rebuild.</strong> A "
                   f"description that does not compile is not usable as an artifact.")
    if getattr(result, "externals", None):
        names = ", ".join(e["contract"] for e in result.externals)
        out.append(f"<strong>{len(result.externals)} external dependency(ies)</strong> — "
                   f"{_esc(names)}. This system calls things it does not own, so the design "
                   f"describes only the part we can see.")
    if design is not None and not derive_integration_checks(design):
        out.append("<strong>No integration surface</strong> — no contract and no frontend root, "
                   "so nothing in this design can be probed against a running system. A later "
                   "<code>update-system</code> run on this directory will build successfully and "
                   "still report <code>integration_failed</code>, because it proved nothing. "
                   "Mapping the services is what creates those checks; inventing a route here "
                   "would manufacture a false pass.")
    if result.status == "gate_failed":
        out.append(f"<strong>A gate failed:</strong> {_esc(result.detail)}")
    return out


def _externals(result) -> str:
    """What the system calls and does not own — the integration surface a reviewer must know."""
    items = getattr(result, "externals", None)
    if not items:
        return "<p class='muted'>none detected.</p>"
    rows = "".join(f"<tr><td><code>{_esc(e['contract'])}</code></td>"
                   f"<td class='muted'>{_esc(', '.join(e['consumed_by']))}</td></tr>"
                   for e in items)
    return f"<table><tr><th>contract</th><th>consumed by</th></tr>{rows}</table>"


def write_report(result, path=None) -> Path:
    """Render the adoption to HTML. Returns the path written."""
    out = Path(path) if path else Path(result.run_dir) / "report.html"
    survey, design, counts = result.survey, result.design, result.counts
    by_service = {v.service: v for v in result.verdicts}

    rows = "".join(_service_row(n, by_service.get(n.name), result.run_dir)
                   for n in (design.services if design else []))
    caveats = "".join(f"<p>{c}</p>" for c in _caveats(result)) \
        or "<p class='muted'>No caveats recorded.</p>"
    gaps = ("<ul>" + "".join(f"<li class='gap'>{_esc(g)}</li>" for g in survey.gaps) + "</ul>"
            if survey.gaps else
            "<p class='muted'>none — every surveyed service matched a recipe.</p>")
    notes = "".join(f"<li class='muted'>{_esc(n)}</li>" for n in survey.notes)
    libs = _esc(", ".join(s.name for s in survey.libraries)) or "none"
    title = _esc(result.title or "Adoption")

    body = (
        f"<h1>{title}</h1>"
        f"<p class='sub'>{_esc(survey.repo)} — status <strong>{_esc(result.status)}</strong>"
        f" · proven {counts.get('proven', 0)}"
        f" · unproven {counts.get('unproven', 0)}"
        f" · unmappable {counts.get('unmappable', 0)}</p>"
        "<div class='gate'><p><strong>This adoption is not truth until a human says so.</strong> "
        "Review this page and <code>design.json</code> before anything downstream consumes them. "
        "Nothing here was written into the source repo.</p>"
        f"{caveats}</div>"
        "<h2>Services</h2><table>"
        "<tr><th>id</th><th>kind</th><th>recipe</th><th>verdict</th><th>detail</th></tr>"
        f"{rows or '<tr><td colspan=5 class=muted>none</td></tr>'}</table>"
        f"<h2>External dependencies</h2>{_externals(result)}"
        f"<h2>Recipe gaps</h2>{gaps}"
        f"<h2>Libraries (not services)</h2><p class='muted'>{libs}</p>"
        f"<h2>What the survey found</h2><ul>{notes}</ul>")

    out.write_text(f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
                   f"<style>{_CSS}</style>{body}")
    return out
