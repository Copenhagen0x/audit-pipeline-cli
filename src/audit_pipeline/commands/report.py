"""`audit-pipeline report` — HTML report generator from findings DB.

Two reports:
  cycle  : single hunt-cycle report
  weekly : rolling 7-day summary across all cycles for a target

Pure stdlib (no Jinja, no Flask) — emits a self-contained HTML file
with inline CSS so it can be served from any static host or attached
to an email.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB
from audit_pipeline.severity import Severity, DEFINITIONS, color_html, emoji as sev_emoji

console = Console()


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #1a1a1a;
       background: #fafafa; }
h1, h2, h3 { color: #0f0f0f; }
h1 { border-bottom: 3px solid #1a1a1a; padding-bottom: .3em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; background: white; }
th, td { padding: .5em .75em; border-bottom: 1px solid #e5e5e5; text-align: left;
         font-size: .92em; }
th { background: #f0f0f0; font-weight: 600; }
.sev { display: inline-block; padding: .15em .55em; border-radius: 4px;
       color: white; font-weight: 600; font-size: .8em; }
.kpi { display: inline-block; background: white; border: 1px solid #d4d4d4;
       padding: 1em 1.5em; margin: .3em; border-radius: 6px; min-width: 120px; }
.kpi .label { font-size: .8em; color: #666; text-transform: uppercase; letter-spacing: .05em; }
.kpi .value { font-size: 1.8em; font-weight: 700; color: #0f0f0f; }
.kpi.danger .value { color: #dc2626; }
.muted { color: #6b7280; font-size: .9em; }
code { background: #f3f4f6; padding: 0 .3em; border-radius: 3px; font-size: .9em; }
.footer { margin-top: 3em; padding-top: 1em; border-top: 1px solid #e5e5e5;
          color: #6b7280; font-size: .85em; }
"""


@click.group(name="report")
def report_cmd() -> None:
    """Generate HTML reports from the findings DB."""


@report_cmd.command(name="cycle")
@click.option("--cycle-id", required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.pass_context
def cycle_report(ctx: click.Context, cycle_id: str, output: Path | None) -> None:
    """Generate an HTML report for a single hunt cycle."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")

    findings = [f for f in db.list_findings(limit=1000) if f.get("cycle_id") == cycle_id]
    if not findings:
        raise click.ClickException(f"No findings for cycle {cycle_id}")

    cycles = db.list_cycles()
    cycle = next((c for c in cycles if c["cycle_id"] == cycle_id), None)
    target_id = cycle["target_id"] if cycle else findings[0]["target_id"]
    target = next((t for t in db.list_targets() if t["id"] == target_id), {"name": "?"})

    out = output or (workspace / "hunts" / cycle_id / "hunt_report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_cycle_html(target, cycle, findings), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


@report_cmd.command(name="weekly")
@click.option("--target", required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--days", type=int, default=7, show_default=True)
@click.pass_context
def weekly_report(ctx: click.Context, target: str, output: Path | None, days: int) -> None:
    """Rolling N-day summary across all cycles for one target."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")
    t = db.get_target(target)
    if not t:
        raise click.ClickException(f"Target '{target}' not found in DB")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cycles = [c for c in db.list_cycles(target_id=t["id"], limit=500)
              if (c.get("started_at") or "") >= cutoff]
    findings = [f for f in db.list_findings(target_id=t["id"], limit=1000)
                if (f.get("created_at") or "") >= cutoff]

    out = output or (workspace / "reports" / f"{target}_weekly_{datetime.now(timezone.utc):%Y%m%d}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_weekly_html(t, cycles, findings, days), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


def _render_cycle_html(target: dict, cycle: dict | None, findings: list[dict]) -> str:
    by_sev = {s.value: 0 for s in Severity}
    for f in findings:
        s = f.get("severity")
        if s in by_sev:
            by_sev[s] += 1
    n_confirmed = sum(1 for f in findings if f.get("status") == "confirmed")

    target_name = html.escape(target.get("name", "?"))
    cycle_id = html.escape(cycle.get("cycle_id", "?") if cycle else "?")
    engine_sha = html.escape((cycle.get("engine_sha") or "?")[:10] if cycle else "?")
    wrapper_sha = html.escape((cycle.get("wrapper_sha") or "?")[:10] if cycle else "?")
    started = html.escape(cycle.get("started_at", "?") if cycle else "?")
    cost = float(cycle.get("total_cost_usd") or 0) if cycle else 0

    rows = []
    for f in sorted(findings, key=lambda x: list(Severity).index(Severity(x["severity"])) if x.get("severity") in [s.value for s in Severity] else 99):
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        rows.append(
            f"<tr><td><span class='sev' style='background:{color_html(sev)}'>"
            f"{sev_emoji(sev)} {sev.value}</span></td>"
            f"<td><code>{html.escape(f.get('hypothesis_id', '?'))}</code></td>"
            f"<td>{html.escape((f.get('title') or '')[:90])}</td>"
            f"<td>{html.escape(f.get('verdict', '?'))} / {html.escape(f.get('confidence', '?'))}</td>"
            f"<td>{html.escape(f.get('status', '?'))}</td>"
            f"<td>{'✅' if f.get('poc_fired') else '—'}</td></tr>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Hunt cycle {cycle_id} — {target_name}</title>
<style>{CSS}</style></head><body>
<h1>Hunt cycle <code>{cycle_id}</code></h1>
<p class="muted">Target: <strong>{target_name}</strong> &middot;
   Started: {started} &middot;
   Engine: <code>{engine_sha}</code> &middot;
   Wrapper: <code>{wrapper_sha}</code> &middot;
   Cost: ${cost:.3f}</p>

<div>
  <div class="kpi {'danger' if by_sev['Critical'] or by_sev['High'] else ''}">
    <div class="label">Confirmed</div><div class="value">{n_confirmed}</div></div>
  <div class="kpi"><div class="label">Critical</div><div class="value">{by_sev['Critical']}</div></div>
  <div class="kpi"><div class="label">High</div><div class="value">{by_sev['High']}</div></div>
  <div class="kpi"><div class="label">Medium</div><div class="value">{by_sev['Medium']}</div></div>
  <div class="kpi"><div class="label">Low</div><div class="value">{by_sev['Low']}</div></div>
  <div class="kpi"><div class="label">Total</div><div class="value">{len(findings)}</div></div>
</div>

<h2>Findings</h2>
<table>
  <thead><tr><th>Severity</th><th>Hypothesis</th><th>Title</th><th>Verdict</th>
    <th>Status</th><th>PoC</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>

<div class="footer">
  Generated by <a href="https://github.com/Copenhagen0x/audit-pipeline-cli">audit-pipeline</a>
  &middot; {datetime.now(timezone.utc).isoformat(timespec='seconds')}
</div>
</body></html>"""


def _render_weekly_html(
    target: dict, cycles: list[dict], findings: list[dict], days: int,
) -> str:
    target_name = html.escape(target.get("name", "?"))
    by_sev = {s.value: 0 for s in Severity}
    for f in findings:
        s = f.get("severity")
        if s in by_sev:
            by_sev[s] += 1
    total_cost = sum(float(c.get("total_cost_usd") or 0) for c in cycles)
    total_confirmed = sum(int(c.get("n_confirmed") or 0) for c in cycles)

    cycle_rows = []
    for c in sorted(cycles, key=lambda x: x.get("started_at") or "", reverse=True):
        cycle_rows.append(
            f"<tr><td><code>{html.escape(c.get('cycle_id', '?'))}</code></td>"
            f"<td>{html.escape(c.get('started_at', '?'))}</td>"
            f"<td>{html.escape((c.get('engine_sha') or '?')[:10])}</td>"
            f"<td>{c.get('n_dispatched', 0)}</td>"
            f"<td>{c.get('n_confirmed', 0)}</td>"
            f"<td>${float(c.get('total_cost_usd') or 0):.2f}</td></tr>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{target_name} — {days}-day audit summary</title>
<style>{CSS}</style></head><body>
<h1>{target_name} — {days}-day audit summary</h1>
<p class="muted">Generated {datetime.now(timezone.utc).isoformat(timespec='minutes')}</p>

<div>
  <div class="kpi"><div class="label">Hunt cycles</div><div class="value">{len(cycles)}</div></div>
  <div class="kpi {'danger' if total_confirmed else ''}">
    <div class="label">Confirmed</div><div class="value">{total_confirmed}</div></div>
  <div class="kpi"><div class="label">Critical+High</div>
    <div class="value">{by_sev['Critical'] + by_sev['High']}</div></div>
  <div class="kpi"><div class="label">Total findings</div><div class="value">{len(findings)}</div></div>
  <div class="kpi"><div class="label">Spend</div><div class="value">${total_cost:.2f}</div></div>
</div>

<h2>Severity breakdown</h2>
<table>
  <thead><tr><th>Severity</th><th>Definition</th><th>Count</th></tr></thead>
  <tbody>
"""+ "".join(
        f"<tr><td><span class='sev' style='background:{color_html(s)}'>{sev_emoji(s)} {s.value}</span></td>"
        f"<td class='muted'>{html.escape(DEFINITIONS[s])}</td>"
        f"<td><strong>{by_sev[s.value]}</strong></td></tr>"
        for s in Severity
    ) + f"""
  </tbody>
</table>

<h2>Hunt cycles ({len(cycles)})</h2>
<table>
  <thead><tr><th>Cycle</th><th>When</th><th>Engine SHA</th><th>Dispatched</th>
    <th>Confirmed</th><th>Cost</th></tr></thead>
  <tbody>{''.join(cycle_rows)}</tbody>
</table>

<div class="footer">
  Generated by <a href="https://github.com/Copenhagen0x/audit-pipeline-cli">audit-pipeline</a>
</div>
</body></html>"""
