"""`audit-pipeline report` — HTML report generator from findings DB.

Two reports:
  cycle  : single hunt-cycle report with executive summary
  weekly : rolling N-day summary across all cycles for a target

Pure stdlib (no Jinja, no Flask) — emits a self-contained HTML file
using the shared Jelleo design system (audit_pipeline.branding).
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.branding import (
    CSS,
    cover_page_html,
    footer_html,
    read_pubkey_fingerprint,
    topbar_html,
)
from audit_pipeline.commands.sign import SignError, default_key_path, sign_file
from audit_pipeline.db import FindingsDB
from audit_pipeline.severity import DEFINITIONS, Severity

console = Console()


def _auto_sign(workspace: Path, report_path: Path, sign_enabled: bool) -> None:
    """Sign a generated report if signing is enabled and a key exists.

    Failures are warnings, not errors — a missing key should not block
    report generation. Customers without keys still get the HTML; the
    .sig file appears next to the report only when a key is configured.
    """
    if not sign_enabled:
        return
    key_path = default_key_path(workspace)
    if not key_path.exists():
        console.print(
            f"[yellow]auto-sign skipped:[/yellow] no key at {key_path}. "
            f"Run [cyan]audit-pipeline sign keygen[/cyan] to enable signed receipts."
        )
        return
    try:
        sig_path = sign_file(report_path, key_path)
    except SignError as e:
        console.print(f"[yellow]auto-sign failed:[/yellow] {e}")
        return
    console.print(f"[green]signed[/green]    {sig_path}")


@click.group(name="report")
def report_cmd() -> None:
    """Generate HTML reports from the findings DB."""


@report_cmd.command(name="cycle")
@click.option("--cycle-id", required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Auto-sign the generated report with the workspace's Ed25519 key.")
@click.pass_context
def cycle_report(
    ctx: click.Context, cycle_id: str, output: Path | None, sign: bool,
) -> None:
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
    pubkey = read_pubkey_fingerprint(workspace)
    out.write_text(_render_cycle_html(target, cycle, findings, pubkey), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")
    _auto_sign(workspace, out, sign)


@report_cmd.command(name="weekly")
@click.option("--target", required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--days", type=int, default=7, show_default=True)
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Auto-sign the generated report with the workspace's Ed25519 key.")
@click.pass_context
def weekly_report(
    ctx: click.Context, target: str, output: Path | None, days: int, sign: bool,
) -> None:
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
    pubkey = read_pubkey_fingerprint(workspace)
    out.write_text(_render_weekly_html(t, cycles, findings, days, pubkey), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")
    _auto_sign(workspace, out, sign)


# ---------------------------------------------------------------------------
# HTML render helpers
# ---------------------------------------------------------------------------


def _sev_counts(findings: list[dict]) -> dict[str, int]:
    by = {s.value: 0 for s in Severity}
    for f in findings:
        s = f.get("severity")
        if s in by:
            by[s] += 1
    return by


def _sev_bar(counts: dict[str, int]) -> str:
    total = max(1, sum(counts.values()))
    if sum(counts.values()) == 0:
        return ""
    return f"""
    <div class="sev-bar">
      <span class="b-critical" style="width:{counts['Critical']/total*100:.1f}%"></span>
      <span class="b-high"     style="width:{counts['High']/total*100:.1f}%"></span>
      <span class="b-medium"   style="width:{counts['Medium']/total*100:.1f}%"></span>
      <span class="b-low"      style="width:{counts['Low']/total*100:.1f}%"></span>
      <span class="b-info"     style="width:{counts['Info']/total*100:.1f}%"></span>
    </div>
    <div class="sev-bar-legend">
      <span><i style="background:var(--critical)"></i>Critical {counts['Critical']}</span>
      <span><i style="background:var(--high)"></i>High {counts['High']}</span>
      <span><i style="background:var(--medium)"></i>Medium {counts['Medium']}</span>
      <span><i style="background:var(--low)"></i>Low {counts['Low']}</span>
      <span><i style="background:var(--info)"></i>Info {counts['Info']}</span>
    </div>"""


def _findings_table(findings: list[dict]) -> str:
    if not findings:
        return '<div class="empty">No findings in this scope.</div>'
    sev_order = {s.value: i for i, s in enumerate(Severity)}
    rows = []
    for f in sorted(findings, key=lambda x: sev_order.get(x.get("severity", "Info"), 99)):
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        status = (f.get("status") or "?").lower()
        rows.append(f"""
        <tr>
          <td><span class="sev {sev_cls}">{sev.value}</span></td>
          <td><code>{html.escape(f.get('hypothesis_id', '?'))}</code></td>
          <td style="max-width:520px">{html.escape((f.get('title') or '')[:160])}</td>
          <td>{html.escape(f.get('verdict','?'))} <span style="color:var(--text-3)">/ {html.escape(f.get('confidence','?'))}</span></td>
          <td><span class="status-pill {status}">{html.escape(status)}</span></td>
          <td>{'<span style="color:var(--ok)">✓ fired</span>' if f.get('poc_fired') else '<span style="color:var(--text-3)">—</span>'}</td>
        </tr>""")
    return f"""
    <table>
      <thead><tr>
        <th>Severity</th><th>Hypothesis</th><th>Title</th>
        <th>Verdict</th><th>Status</th><th>PoC</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _render_cycle_html(
    target: dict,
    cycle: dict | None,
    findings: list[dict],
    pubkey_fingerprint: str = "",
) -> str:
    target_name = html.escape(target.get("name", "?"))
    cycle_id = html.escape(cycle.get("cycle_id", "?") if cycle else "?")
    engine_sha = html.escape((cycle.get("engine_sha") or "?")[:10] if cycle else "?")
    wrapper_sha = html.escape((cycle.get("wrapper_sha") or "?")[:10] if cycle else "?")
    started = html.escape(cycle.get("started_at", "?") if cycle else "?")

    counts = _sev_counts(findings)
    n_confirmed = sum(1 for f in findings if f.get("status") == "confirmed")

    if counts["Critical"] > 0:
        status_label, status_class = f"{counts['Critical']} Critical · disclosure required", "critical"
    elif counts["High"] > 0:
        status_label, status_class = f"{counts['High']} High · review required", "warn"
    else:
        status_label, status_class = "Cycle complete · no Critical/High", "ok"

    cover = cover_page_html(
        target_name=target_name,
        report_title="Hunt cycle ·",
        window_label=f"cycle {cycle_id}",
        cycle_id=cycle_id,
        engine_sha=engine_sha,
        wrapper_sha=wrapper_sha,
        severity_counts=counts,
        pubkey_fingerprint=pubkey_fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JELLEO · {target_name} · cycle {cycle_id}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head><body>

{topbar_html(status_label, status_class)}

{cover}

<div class="shell">

  <h1>{target_name} · hunt cycle</h1>
  <p class="subhead">
    <code>{cycle_id}</code> &middot;
    started {started} &middot;
    engine <code>{engine_sha}</code> &middot;
    wrapper <code>{wrapper_sha}</code>
  </p>

  <div class="kpi-grid">
    <div class="kpi {'danger' if counts['Critical'] else 'ok'}">
      <div class="label">Critical</div><div class="value">{counts['Critical']}</div></div>
    <div class="kpi {'warn' if counts['High'] else 'ok'}">
      <div class="label">High</div><div class="value">{counts['High']}</div></div>
    <div class="kpi"><div class="label">Medium</div><div class="value">{counts['Medium']}</div></div>
    <div class="kpi"><div class="label">Confirmed</div><div class="value">{n_confirmed}</div></div>
    <div class="kpi"><div class="label">Total verdicts</div><div class="value">{len(findings)}</div></div>
  </div>

  {_sev_bar(counts)}

  <h2>Findings</h2>
  {_findings_table(findings)}

  <h2>Severity rubric</h2>
  <table>
    <thead><tr><th style="width:120px">Tier</th><th>Definition</th></tr></thead>
    <tbody>{''.join(
        f'<tr><td><span class="sev {s.value.lower()}">{s.value}</span></td>'
        f'<td style="color:var(--text-2)">{html.escape(DEFINITIONS[s])}</td></tr>'
        for s in Severity
    )}</tbody>
  </table>

  {footer_html(extra=f"Cycle {cycle_id}")}

</div>
</body></html>"""


def _render_weekly_html(
    target: dict, cycles: list[dict], findings: list[dict], days: int,
    pubkey_fingerprint: str = "",
) -> str:
    target_name = html.escape(target.get("name", "?"))
    counts = _sev_counts(findings)
    total_confirmed = sum(int(c.get("n_confirmed") or 0) for c in cycles)

    if counts["Critical"] > 0:
        status_label, status_class = f"{counts['Critical']} Critical this period", "critical"
    elif counts["High"] > 0:
        status_label, status_class = f"{counts['High']} High this period", "warn"
    else:
        status_label, status_class = f"Active · {days}-day window", "ok"

    # Window label: e.g. "24-hour rollup" / "7-day rollup" / "30-day rollup"
    if days == 1:
        window_label = "24-hour rollup"
        report_title = "24-hour audit ·"
    elif days <= 7:
        window_label = f"{days}-day rollup"
        report_title = f"{days}-day audit ·"
    else:
        window_label = f"{days}-day rollup"
        report_title = "Monthly audit ·"

    most_recent_cycle = sorted(
        cycles, key=lambda x: x.get("started_at") or "", reverse=True
    )[0] if cycles else None

    cover = cover_page_html(
        target_name=target_name,
        report_title=report_title,
        window_label=window_label,
        cycle_id="",
        engine_sha=(most_recent_cycle.get("engine_sha") or "")[:10] if most_recent_cycle else "",
        wrapper_sha=(most_recent_cycle.get("wrapper_sha") or "")[:10] if most_recent_cycle else "",
        severity_counts=counts,
        pubkey_fingerprint=pubkey_fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    cycle_rows = []
    for c in sorted(cycles, key=lambda x: x.get("started_at") or "", reverse=True):
        cycle_rows.append(f"""
        <tr>
          <td><code>{html.escape(c.get('cycle_id', '?'))}</code></td>
          <td class="mono" style="color:var(--text-2)">{html.escape(c.get('started_at', '?'))}</td>
          <td><code>{html.escape((c.get('engine_sha') or '?')[:10])}</code></td>
          <td class="num">{c.get('n_dispatched', 0)}</td>
          <td class="num">{c.get('n_confirmed', 0)}</td>
        </tr>""")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JELLEO · {target_name} · {days}-day report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head><body>

{topbar_html(status_label, status_class)}

{cover}

<div class="shell">

  <h1>{target_name} · {days}-day audit summary</h1>
  <p class="subhead">{datetime.now(timezone.utc).isoformat(timespec='minutes')} · rolling window</p>

  <div class="kpi-grid">
    <div class="kpi {'danger' if counts['Critical'] else 'ok'}">
      <div class="label">Critical</div><div class="value">{counts['Critical']}</div></div>
    <div class="kpi {'warn' if counts['High'] else 'ok'}">
      <div class="label">High</div><div class="value">{counts['High']}</div></div>
    <div class="kpi"><div class="label">Medium</div><div class="value">{counts['Medium']}</div></div>
    <div class="kpi"><div class="label">Hunt cycles</div><div class="value">{len(cycles)}</div></div>
    <div class="kpi"><div class="label">Confirmed</div><div class="value">{total_confirmed}</div></div>
  </div>

  {_sev_bar(counts)}

  <h2>Severity rubric</h2>
  <table>
    <thead><tr><th style="width:120px">Tier</th><th>Definition</th></tr></thead>
    <tbody>{''.join(
        f'<tr><td><span class="sev {s.value.lower()}">{s.value}</span></td>'
        f'<td style="color:var(--text-2)">{html.escape(DEFINITIONS[s])}</td></tr>'
        for s in Severity
    )}</tbody>
  </table>

  <h2>Hunt cycles ({len(cycles)})</h2>
  <table>
    <thead><tr>
      <th>Cycle</th><th>Started (UTC)</th><th>Engine SHA</th>
      <th class="num">Dispatched</th><th class="num">Confirmed</th>
    </tr></thead>
    <tbody>{''.join(cycle_rows) or '<tr><td colspan="5" class="empty">No cycles in window.</td></tr>'}</tbody>
  </table>

  <h2>Findings ({len(findings)})</h2>
  {_findings_table(findings)}

  {footer_html(extra=f"{days}-day rolling")}

</div>
</body></html>"""
