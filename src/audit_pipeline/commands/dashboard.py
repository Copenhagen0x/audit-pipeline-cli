"""`audit-pipeline dashboard` — single-file HTML status dashboard.

Reads the findings DB and emits a self-contained HTML page showing:
  - All targets currently being audited
  - Recent hunt cycles per target
  - Severity breakdown
  - Recent findings table
  - Daemon status (last successful cycle / last alert)

Two modes:
  generate : write static HTML to a file
  serve    : write + serve via stdlib http.server on a port
"""

from __future__ import annotations

import html
import http.server
import socketserver
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB
from audit_pipeline.severity import Severity, color_html, emoji as sev_emoji

console = Console()


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 1400px; margin: 1.5em auto; padding: 0 1em; color: #1a1a1a;
       background: #f5f5f5; }
h1 { border-bottom: 3px solid #1a1a1a; padding-bottom: .3em; margin-top: 0; }
h2 { margin-top: 2em; color: #2a2a2a; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; background: white;
        box-shadow: 0 1px 2px rgba(0,0,0,.05); }
th, td { padding: .5em .75em; border-bottom: 1px solid #e5e5e5; text-align: left;
         font-size: .9em; }
th { background: #1a1a1a; color: white; font-weight: 600; }
tr:hover { background: #f9fafb; }
.sev { display: inline-block; padding: .15em .55em; border-radius: 4px;
       color: white; font-weight: 600; font-size: .8em; }
.kpi-grid { display: flex; flex-wrap: wrap; gap: .8em; margin: 1em 0; }
.kpi { background: white; border: 1px solid #d4d4d4; padding: 1em 1.5em;
       border-radius: 6px; min-width: 130px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.kpi .label { font-size: .75em; color: #666; text-transform: uppercase;
              letter-spacing: .05em; }
.kpi .value { font-size: 2em; font-weight: 700; color: #0f0f0f; line-height: 1.2; }
.kpi.danger .value { color: #dc2626; }
.kpi.ok .value { color: #16a34a; }
.muted { color: #6b7280; font-size: .85em; }
code { background: #f3f4f6; padding: 0 .3em; border-radius: 3px; font-size: .9em; }
.badge { display: inline-block; padding: .1em .5em; border-radius: 10px;
         background: #e5e7eb; color: #374151; font-size: .75em; font-weight: 500; }
.target-card { background: white; padding: 1em 1.5em; margin: .5em 0;
               border-radius: 6px; border: 1px solid #d4d4d4; }
.target-card h3 { margin-top: 0; }
.footer { margin-top: 3em; padding-top: 1em; border-top: 1px solid #e5e5e5;
          color: #6b7280; font-size: .8em; text-align: center; }
.refresh { font-size: .8em; color: #6b7280; }
"""


@click.command(name="dashboard")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="HTML file to write (default: <workspace>/dashboard.html)")
@click.option("--serve", is_flag=True, help="Serve via http.server after writing")
@click.option("--port", type=int, default=8765, show_default=True)
@click.option("--auto-refresh", type=int, default=60, show_default=True,
              help="Browser auto-refresh interval (seconds)")
@click.pass_context
def dashboard_cmd(
    ctx: click.Context, output: Path | None, serve: bool, port: int, auto_refresh: int,
) -> None:
    """Generate (and optionally serve) the customer-facing dashboard."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")

    out = output or (workspace / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(db, auto_refresh), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")

    if serve:
        _serve(out.parent, out.name, port)


def _render(db: FindingsDB, auto_refresh: int) -> str:
    stats = db.stats()
    targets = db.list_targets()
    cycles = db.list_cycles(limit=20)
    findings = db.list_findings(limit=50)

    n_critical = stats["by_severity"].get("Critical", 0)
    n_high = stats["by_severity"].get("High", 0)

    target_cards = []
    for t in targets:
        t_cycles = [c for c in db.list_cycles(target_id=t["id"], limit=5)]
        t_findings = db.list_findings(target_id=t["id"], limit=200)
        t_critical = sum(1 for f in t_findings if f.get("severity") == "Critical")
        t_high = sum(1 for f in t_findings if f.get("severity") == "High")
        last_cycle = t_cycles[0] if t_cycles else None
        last_at = last_cycle["started_at"] if last_cycle else "never"
        target_cards.append(
            f"""<div class="target-card">
              <h3>{html.escape(t['name'])}
                <span class="badge">{len(t_findings)} findings</span>
                {('<span class="badge" style="background:#fee2e2;color:#991b1b">'
                  + str(t_critical + t_high) + ' Critical+High</span>') if (t_critical + t_high) else ''}
              </h3>
              <div class="muted">
                Repo: <code>{html.escape((t.get('engine_repo') or '?'))}</code><br>
                Last cycle: {html.escape(last_at)} &middot;
                Total cycles: {len(t_cycles)}
              </div>
            </div>"""
        )

    cycle_rows = []
    for c in cycles:
        cycle_rows.append(
            f"<tr><td><code>{html.escape(c.get('cycle_id', '?'))}</code></td>"
            f"<td>{html.escape(c.get('started_at', '?'))}</td>"
            f"<td>{html.escape((c.get('engine_sha') or '?')[:10])}</td>"
            f"<td>{c.get('n_dispatched', 0)}</td>"
            f"<td>{c.get('n_confirmed', 0)}</td>"
            f"<td>${float(c.get('total_cost_usd') or 0):.3f}</td></tr>"
        )

    finding_rows = []
    for f in findings[:30]:
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        finding_rows.append(
            f"<tr><td><span class='sev' style='background:{color_html(sev)}'>"
            f"{sev_emoji(sev)} {sev.value}</span></td>"
            f"<td><code>{html.escape(f.get('hypothesis_id', '?'))}</code></td>"
            f"<td>{html.escape((f.get('title') or '')[:80])}</td>"
            f"<td>{html.escape(f.get('status', '?'))}</td>"
            f"<td>{'✅' if f.get('poc_fired') else '—'}</td>"
            f"<td>{html.escape(f.get('updated_at', '?'))}</td></tr>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sentinel — Audit Dashboard</title>
<meta http-equiv="refresh" content="{auto_refresh}">
<style>{CSS}</style></head><body>
<h1>🛡️ Sentinel</h1>
<p class="muted">
  Autonomous Solana audit pipeline &middot;
  <span class="refresh">refreshes every {auto_refresh}s</span> &middot;
  generated {datetime.now(timezone.utc).isoformat(timespec='minutes')}
</p>

<div class="kpi-grid">
  <div class="kpi"><div class="label">Targets</div><div class="value">{stats['n_targets']}</div></div>
  <div class="kpi"><div class="label">Hunt cycles</div><div class="value">{stats['n_cycles']}</div></div>
  <div class="kpi {'danger' if n_critical else 'ok'}">
    <div class="label">Critical</div><div class="value">{n_critical}</div></div>
  <div class="kpi {'danger' if n_high else 'ok'}">
    <div class="label">High</div><div class="value">{n_high}</div></div>
  <div class="kpi"><div class="label">Total findings</div>
    <div class="value">{stats['n_findings']}</div></div>
</div>

<h2>Targets ({len(targets)})</h2>
{''.join(target_cards) or '<p class="muted">No targets yet. Run <code>audit-pipeline init</code> or <code>audit-pipeline onboard</code>.</p>'}

<h2>Recent hunt cycles</h2>
<table>
  <thead><tr><th>Cycle ID</th><th>Started</th><th>Engine SHA</th>
    <th>Dispatched</th><th>Confirmed</th><th>Cost</th></tr></thead>
  <tbody>{''.join(cycle_rows) or '<tr><td colspan="6" class="muted">No cycles yet</td></tr>'}</tbody>
</table>

<h2>Recent findings</h2>
<table>
  <thead><tr><th>Severity</th><th>Hypothesis</th><th>Title</th><th>Status</th>
    <th>PoC</th><th>Updated</th></tr></thead>
  <tbody>{''.join(finding_rows) or '<tr><td colspan="6" class="muted">No findings yet</td></tr>'}</tbody>
</table>

<div class="footer">
  Powered by <a href="https://github.com/Copenhagen0x/audit-pipeline-cli">audit-pipeline</a>
</div>
</body></html>"""


def _serve(directory: Path, default_file: str, port: int) -> None:
    handler = type(
        "Handler", (http.server.SimpleHTTPRequestHandler,),
        {"directory": str(directory)},
    )
    # Newer SimpleHTTPRequestHandler accepts directory in __init__
    class _H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

    with socketserver.TCPServer(("0.0.0.0", port), _H) as httpd:
        console.print(f"[bold]Serving[/bold] {directory}/{default_file} on http://0.0.0.0:{port}/")
        console.print(f"  open: http://0.0.0.0:{port}/{default_file}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("[yellow]stopped[/yellow]")
