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

The page uses the shared Jelleo design system (audit_pipeline.branding).
"""

from __future__ import annotations

import html
import http.server
import socketserver
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.branding import CSS, footer_html, topbar_html
from audit_pipeline.db import FindingsDB
from audit_pipeline.severity import Severity

console = Console()


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
    n_medium = stats["by_severity"].get("Medium", 0)
    n_low = stats["by_severity"].get("Low", 0)
    n_info = stats["by_severity"].get("Info", 0)
    n_confirmed = stats["by_status"].get("confirmed", 0)
    n_open = (n_critical + n_high) - stats["by_status"].get("fixed", 0) - stats["by_status"].get("verified", 0)

    # Status pill: critical if any open Critical+High, warn if Medium, ok otherwise
    if n_critical > 0:
        status_label, status_class = "Critical findings open", "critical"
    elif n_high > 0:
        status_label, status_class = "High findings open", "warn"
    else:
        status_label, status_class = "Active · monitoring", "ok"

    # ---------- Target cards ----------
    target_cards = []
    for t in targets:
        t_cycles = db.list_cycles(target_id=t["id"], limit=5)
        t_findings = db.list_findings(target_id=t["id"], limit=500)
        t_critical = sum(1 for f in t_findings if f.get("severity") == "Critical")
        t_high = sum(1 for f in t_findings if f.get("severity") == "High")
        last_cycle = t_cycles[0] if t_cycles else None
        last_at = last_cycle["started_at"] if last_cycle else "—"
        repo = (t.get("engine_repo") or "").replace("https://github.com/", "")
        sev_open_html = ""
        if t_critical:
            sev_open_html += f'<span class="sev critical">{t_critical} Critical</span>'
        if t_high:
            sev_open_html += f' <span class="sev high">{t_high} High</span>'
        target_cards.append(f"""
        <div class="card">
          <div class="row">
            <div>
              <h3>{html.escape(t['name'])}</h3>
              <div class="meta">{html.escape(repo) or '—'}</div>
            </div>
            <div style="text-align:right">
              {sev_open_html}
              <div class="meta" style="margin-top:6px">
                {len(t_findings)} findings · {len(t_cycles)} cycles
              </div>
            </div>
          </div>
          <div class="meta" style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px">
            Last cycle <code>{html.escape(last_at)}</code>
          </div>
        </div>""")

    # ---------- Cycles table ----------
    cycle_rows = []
    for c in cycles:
        cycle_rows.append(f"""
        <tr>
          <td><code>{html.escape(c.get('cycle_id', '?'))}</code></td>
          <td class="mono" style="color:var(--text-2)">{html.escape(c.get('started_at', '—'))}</td>
          <td><code>{html.escape((c.get('engine_sha') or '?')[:10])}</code></td>
          <td class="num">{c.get('n_dispatched', 0)}</td>
          <td class="num">{c.get('n_confirmed', 0) or '<span style="color:var(--text-3)">0</span>'}</td>
        </tr>""")

    # ---------- Findings table ----------
    finding_rows = []
    for f in findings[:30]:
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        status = (f.get("status") or "?").lower()
        finding_rows.append(f"""
        <tr>
          <td><span class="sev {sev_cls}">{sev.value}</span></td>
          <td><code>{html.escape(f.get('hypothesis_id', '?'))}</code></td>
          <td style="max-width:480px">{html.escape((f.get('title') or '')[:140])}</td>
          <td><span class="status-pill {status}">{html.escape(status)}</span></td>
          <td>{'<span style="color:var(--ok)">✓</span>' if f.get('poc_fired') else '<span style="color:var(--text-3)">—</span>'}</td>
          <td class="mono" style="color:var(--text-3)">{html.escape((f.get('updated_at') or '')[:19])}</td>
        </tr>""")

    # ---------- Severity bar ----------
    n_total_sev = max(1, n_critical + n_high + n_medium + n_low + n_info)
    sev_bar = ""
    if (n_critical + n_high + n_medium + n_low + n_info) > 0:
        sev_bar = f"""
        <div class="sev-bar">
          <span class="b-critical" style="width:{n_critical/n_total_sev*100:.1f}%"></span>
          <span class="b-high"     style="width:{n_high/n_total_sev*100:.1f}%"></span>
          <span class="b-medium"   style="width:{n_medium/n_total_sev*100:.1f}%"></span>
          <span class="b-low"      style="width:{n_low/n_total_sev*100:.1f}%"></span>
          <span class="b-info"     style="width:{n_info/n_total_sev*100:.1f}%"></span>
        </div>
        <div class="sev-bar-legend">
          <span><i style="background:var(--critical)"></i>Critical {n_critical}</span>
          <span><i style="background:var(--high)"></i>High {n_high}</span>
          <span><i style="background:var(--medium)"></i>Medium {n_medium}</span>
          <span><i style="background:var(--low)"></i>Low {n_low}</span>
          <span><i style="background:var(--info)"></i>Info {n_info}</span>
        </div>"""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JELLEO · Autonomous Solana audit</title>
<meta http-equiv="refresh" content="{auto_refresh}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head><body>

{topbar_html(status_label, status_class)}

<div class="shell">

  <h1>Operations</h1>
  <p class="subhead">Continuous on-chain &amp; source-code audit across {stats['n_targets']} target{'s' if stats['n_targets'] != 1 else ''} · {stats['n_cycles']} hunt cycles to date</p>

  <div class="kpi-grid">
    <div class="kpi {'danger' if n_critical else 'ok'}">
      <div class="label">Critical</div>
      <div class="value">{n_critical}</div>
      <div class="delta">open findings</div>
    </div>
    <div class="kpi {'warn' if n_high else 'ok'}">
      <div class="label">High</div>
      <div class="value">{n_high}</div>
      <div class="delta">open findings</div>
    </div>
    <div class="kpi">
      <div class="label">Medium</div>
      <div class="value">{n_medium}</div>
      <div class="delta">open findings</div>
    </div>
    <div class="kpi">
      <div class="label">Confirmed</div>
      <div class="value">{n_confirmed}</div>
      <div class="delta">PoC-validated</div>
    </div>
    <div class="kpi">
      <div class="label">Hunt cycles</div>
      <div class="value">{stats['n_cycles']}</div>
      <div class="delta">since deployment</div>
    </div>
  </div>

  {sev_bar}

  <h2>Targets under audit</h2>
  {''.join(target_cards) if target_cards else '<div class="empty">No targets registered. Run <code>audit-pipeline onboard &lt;github-url&gt;</code></div>'}

  <h2>Recent hunt cycles</h2>
  <table>
    <thead><tr>
      <th>Cycle</th><th>Started (UTC)</th><th>Engine SHA</th>
      <th class="num">Dispatched</th><th class="num">Confirmed</th>
    </tr></thead>
    <tbody>{''.join(cycle_rows) or '<tr><td colspan="5" class="empty">No hunt cycles yet.</td></tr>'}</tbody>
  </table>

  <h2>Recent findings</h2>
  <table>
    <thead><tr>
      <th>Severity</th><th>Hypothesis</th><th>Title</th>
      <th>Status</th><th>PoC</th><th>Updated</th>
    </tr></thead>
    <tbody>{''.join(finding_rows) or '<tr><td colspan="6" class="empty">No findings recorded yet.</td></tr>'}</tbody>
  </table>

  {footer_html(extra=datetime.now(timezone.utc).isoformat(timespec='minutes'))}

</div>
</body></html>"""


def _serve(directory: Path, default_file: str, port: int) -> None:
    class _H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

    with socketserver.TCPServer(("0.0.0.0", port), _H) as httpd:
        console.print(f"[bold]Serving[/bold] {directory}/{default_file} on http://0.0.0.0:{port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("[yellow]stopped[/yellow]")
