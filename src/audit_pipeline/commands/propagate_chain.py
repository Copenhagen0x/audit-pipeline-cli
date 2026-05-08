"""`audit-pipeline propagate chain <finding-id>` — render a propagation chain.

Wave 8b deliverable. Walks the artefacts tied to a confirmed finding and
emits a single self-contained HTML page that visualises:

  Parent finding
    │
    ├── Derived siblings (LLM-emitted, from `<workspace>/derived/<slug>-siblings.yaml`)
    │      └── Each sibling: id, severity, applies_to, claim summary, derived bug_class
    │
    ├── Cross-protocol propagation report
    │      └── Signatures used + ranked corpus matches
    │
    └── Layer-1 dispatch queue (E17 — `<workspace>/recon/propagate/scheduled/<id>-*.json`)
           └── Each queued item: target protocol, score, status (pending/dispatched)

Output goes to `<workspace>/recon/propagate/chains/<finding-id>.html`,
self-contained (inline CSS, no external deps), Jelleo-styled. Linkable
from the customer manifest so a per-customer dashboard can surface
"propagation chain for finding X" without round-tripping back to the
operator host.

The HTML is intentionally lean — readable on its own without needing
the rest of the platform.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from audit_pipeline.db import open_findings_db

console = Console()


@click.command(name="chain")
@click.argument("finding_id", type=int)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), default=None,
    help="HTML output path (default: <workspace>/recon/propagate/chains/<id>.html)",
)
@click.pass_context
def chain_cmd(ctx: click.Context, finding_id: int, output: Path | None) -> None:
    """Render the propagation chain for a finding as a Jelleo-styled HTML page."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    finding = db.get_finding(finding_id)
    if not finding:
        raise click.ClickException(f"finding {finding_id} not found in DB")

    # Resolve sibling YAML
    derived_dir = workspace / "derived"
    siblings: list[dict] = []
    sibling_path: Path | None = None
    if derived_dir.is_dir():
        slug = (finding.get("hypothesis_id") or f"finding-{finding_id}").replace("/", "-")
        candidate = derived_dir / f"{slug}-siblings.yaml"
        if candidate.is_file():
            sibling_path = candidate
            try:
                raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    siblings = raw.get("hypotheses") or []
            except Exception as e:
                console.print(f"[yellow]warn: could not parse {candidate}: {e}[/yellow]")

    # Resolve propagation report
    autofire_dir = workspace / "recon" / "propagate" / "auto-fire"
    report_paths = list(autofire_dir.glob(f"propagation_finding_{finding_id}_*.md")) \
                   if autofire_dir.is_dir() else []
    report_path = report_paths[0] if report_paths else None

    # Resolve dispatch queue items for this finding
    queue_dir = workspace / "recon" / "propagate" / "scheduled"
    queue_items: list[dict] = []
    if queue_dir.is_dir():
        for q in queue_dir.glob(f"{finding_id}-*.json"):
            try:
                payload = json.loads(q.read_text(encoding="utf-8"))
                for item in payload.get("items") or []:
                    queue_items.append(item)
            except Exception:
                continue

    # Idempotency marker state
    marker = workspace / "recon" / "propagate" / "markers" / f"{finding_id}.fired"
    fired = marker.is_file()

    # Render
    html_text = _render_chain_html(
        finding=finding,
        siblings=siblings,
        sibling_path=sibling_path,
        report_path=report_path,
        queue_items=queue_items,
        fired=fired,
        workspace=workspace,
    )

    out = output or (workspace / "recon" / "propagate" / "chains" / f"{finding_id}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")
    console.print(
        f"[dim]chain: parent + {len(siblings)} sibling(s) + "
        f"{'1 propagation report' if report_path else '0 reports'} + "
        f"{len(queue_items)} queued dispatch(es)[/dim]"
    )


def _render_chain_html(
    *,
    finding: dict,
    siblings: list[dict],
    sibling_path: Path | None,
    report_path: Path | None,
    queue_items: list[dict],
    fired: bool,
    workspace: Path,
) -> str:
    """Render the chain as a single self-contained HTML page."""
    fid = finding.get("id")
    hyp_id = html.escape(finding.get("hypothesis_id") or f"finding-{fid}")
    title = html.escape((finding.get("title") or "(no title)")[:200])
    severity = html.escape(finding.get("severity") or "Unknown")
    bug_class = html.escape(finding.get("bug_class") or "(unset)")
    status = html.escape(finding.get("status") or "(unknown)")

    sibling_cards = []
    for s in siblings:
        if not isinstance(s, dict):
            continue
        sib_id = html.escape(str(s.get("id") or "?"))
        sib_sev = html.escape(str(s.get("severity") or "?"))
        sib_class = html.escape(str(s.get("class") or "?"))
        sib_bug = html.escape(str(s.get("bug_class") or "?"))
        sib_claim = html.escape(str(s.get("claim") or "")[:300])
        sib_applies = ", ".join(html.escape(str(p)) for p in (s.get("applies_to") or [])[:5])
        sibling_cards.append(f"""
        <div class="sibling-card sev-{sib_sev.lower()}">
          <div class="sibling-head">
            <span class="sibling-id">{sib_id}</span>
            <span class="sibling-sev sev-pill {sib_sev.lower()}">{sib_sev}</span>
            <span class="sibling-class">{sib_class}</span>
          </div>
          <div class="sibling-bug">bug_class: <code>{sib_bug}</code></div>
          <div class="sibling-applies">applies to: {sib_applies or '(any)'}</div>
          <div class="sibling-claim">{sib_claim}</div>
        </div>""")
    siblings_html = "\n".join(sibling_cards) or '<div class="empty">No siblings derived for this finding yet.</div>'

    queue_rows = []
    for q in queue_items[:20]:
        sh = q.get("suggested_hunt", {})
        queue_rows.append(f"""
          <tr>
            <td><code>{html.escape(str(q.get('candidate_repo') or '?'))}</code></td>
            <td><code>{html.escape(str(q.get('candidate_file') or '?'))}</code></td>
            <td>{q.get('candidate_line') or 0}</td>
            <td>{q.get('candidate_score') or 0}</td>
            <td><span class="status-pill {html.escape(str(q.get('status') or '?'))}">{html.escape(str(q.get('status') or '?'))}</span></td>
            <td><code>{html.escape(str(sh.get('bug_class_filter') or '?'))}</code></td>
          </tr>""")
    queue_html = ""
    if queue_rows:
        queue_html = f"""
        <table class="queue">
          <thead><tr><th>Repo</th><th>File</th><th>Line</th><th>Score</th><th>Status</th><th>Filter</th></tr></thead>
          <tbody>{''.join(queue_rows)}</tbody>
        </table>"""
    else:
        queue_html = '<div class="empty">No Layer-1 dispatch items queued for this finding.</div>'

    sibling_link = ""
    if sibling_path:
        # render as a relative anchor for filesystem viewers; on the deployed site, this is just informational
        sibling_link = f'<div class="meta">Source YAML: <code>{html.escape(str(sibling_path.relative_to(workspace)))}</code></div>'

    report_link = ""
    if report_path:
        report_link = f'<div class="meta">Source report: <code>{html.escape(str(report_path.relative_to(workspace)))}</code></div>'

    fired_badge = '<span class="status-pill fired">FIRED</span>' if fired else '<span class="status-pill not-fired">not fired</span>'

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jelleo · propagation chain · {hyp_id}</title>
<style>
  body{{font-family:Inter,system-ui,sans-serif;background:#050504;color:#e6e1d8;margin:0;padding:48px 32px;max-width:980px;margin-inline:auto;line-height:1.55}}
  h1{{color:#f5b800;font-weight:600;letter-spacing:-0.01em;margin:0 0 8px}}
  h2{{color:#f5b800;font-weight:500;font-size:1.05rem;margin:32px 0 12px;letter-spacing:.005em;border-bottom:1px solid rgba(245,184,0,0.18);padding-bottom:8px}}
  p{{color:#bdb5a8;margin:8px 0}}
  a{{color:#f5b800;text-decoration:none;border-bottom:1px dashed rgba(245,184,0,0.3)}}
  a:hover{{border-bottom-style:solid}}
  code{{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.85rem;color:#f5b800;background:rgba(245,184,0,0.06);padding:1px 5px;border-radius:3px}}
  .meta{{color:#7a7163;font-size:0.85rem;font-family:'JetBrains Mono',ui-monospace,monospace;margin:6px 0}}
  .parent-card{{background:rgba(245,184,0,0.04);border:1px solid rgba(245,184,0,0.2);border-left:4px solid #f5b800;border-radius:6px;padding:18px 22px;margin:16px 0 24px}}
  .parent-id{{font-family:'JetBrains Mono',ui-monospace,monospace;color:#f5b800;font-size:0.95rem;font-weight:600}}
  .parent-title{{color:#e6e1d8;margin:8px 0;font-size:1.05rem}}
  .parent-bug{{color:#bdb5a8;font-size:0.9rem}}
  .sibling-card{{background:rgba(245,184,0,0.025);border:1px solid rgba(245,184,0,0.12);border-left:3px solid rgba(245,184,0,0.4);border-radius:6px;padding:14px 18px;margin:10px 0}}
  .sibling-head{{display:flex;gap:12px;align-items:center;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.85rem;margin-bottom:8px}}
  .sibling-id{{color:#f5b800;font-weight:600}}
  .sibling-class{{color:#7a7163;text-transform:uppercase;letter-spacing:0.08em;font-size:0.7rem}}
  .sibling-bug,.sibling-applies{{color:#bdb5a8;font-size:0.85rem;margin:4px 0}}
  .sibling-claim{{color:#d4cdc0;font-size:0.9rem;margin-top:8px;line-height:1.55}}
  .sev-pill{{display:inline-block;padding:1px 8px;border-radius:3px;font-size:0.7rem;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}}
  .sev-pill.critical{{background:rgba(239,68,68,0.12);color:#ef4444;border:1px solid rgba(239,68,68,0.3)}}
  .sev-pill.high{{background:rgba(245,158,11,0.12);color:#f59e0b;border:1px solid rgba(245,158,11,0.3)}}
  .sev-pill.medium{{background:rgba(234,179,8,0.12);color:#eab308;border:1px solid rgba(234,179,8,0.3)}}
  .sev-pill.low,.sev-pill.info{{background:rgba(34,197,94,0.10);color:#22c55e;border:1px solid rgba(34,197,94,0.3)}}
  .status-pill{{display:inline-block;padding:1px 8px;border-radius:3px;font-size:0.7rem;font-family:'JetBrains Mono',ui-monospace,monospace}}
  .status-pill.pending{{background:rgba(245,184,0,0.10);color:#f5b800}}
  .status-pill.dispatched{{background:rgba(34,197,94,0.10);color:#22c55e}}
  .status-pill.fired{{background:rgba(34,197,94,0.10);color:#22c55e}}
  .status-pill.not-fired{{background:rgba(122,113,99,0.18);color:#7a7163}}
  table.queue{{width:100%;border-collapse:collapse;margin:8px 0;font-size:0.85rem}}
  table.queue th{{text-align:left;color:#7a7163;font-weight:500;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.7rem;letter-spacing:0.18em;text-transform:uppercase;padding:10px 12px;border-bottom:1px solid rgba(245,184,0,0.18)}}
  table.queue td{{padding:10px 12px;border-bottom:1px solid rgba(245,184,0,0.06);font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.82rem;color:#e6e1d8}}
  .empty{{color:#7a7163;font-style:italic;padding:14px 0}}
  .chain-tree{{font-family:'JetBrains Mono',ui-monospace,monospace;color:#7a7163;line-height:1.4;font-size:0.85rem;margin:12px 0}}
  hr{{border:0;border-top:1px solid rgba(245,184,0,0.18);margin:32px 0}}
</style>
</head>
<body>

<h1>Propagation chain · {hyp_id}</h1>
<p class="meta">Generated {now} &middot; <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/docs/methodology/04-propagation.md">propagation methodology &sect;04</a></p>

<div class="chain-tree">
  parent finding
   ├── {len(siblings)} derived sibling{'s' if len(siblings) != 1 else ''}
   ├── {1 if report_path else 0} cross-protocol propagation report
   ├── {len(queue_items)} queued Layer-1 dispatch{'es' if len(queue_items) != 1 else ''}
   └── propagation hook: {('FIRED' if fired else 'not fired')}
</div>

<h2>01 &mdash; Parent finding</h2>
<div class="parent-card">
  <div class="parent-id">{hyp_id}</div>
  <div class="parent-title">{title}</div>
  <div class="parent-bug">
    bug_class: <code>{bug_class}</code> &middot;
    severity: <span class="sev-pill {severity.lower()}">{severity}</span> &middot;
    status: <code>{status}</code> &middot;
    propagation: {fired_badge}
  </div>
</div>

<h2>02 &mdash; Derived siblings ({len(siblings)})</h2>
<p>Structural siblings auto-emitted by the LLM at lifecycle <code>confirmed</code> transition.
Each sibling is a falsifiable claim about an invariant adjacent to the parent's bug class.</p>
{sibling_link}
{siblings_html}

<h2>03 &mdash; Cross-protocol propagation</h2>
{(f'<p>Top corpus candidates after sweeping signatures registered for <code>{bug_class}</code>:</p>' + report_link) if report_path else '<div class="empty">No propagation report for this finding yet.</div>'}

<h2>04 &mdash; Layer-1 dispatch queue</h2>
<p>Each queued item is a suggested Layer-1 hunt against a corpus candidate that scored high
on the parent's bug class signatures. Operator runs <code>audit-pipeline propagate dispatch-pending</code>
to fire them.</p>
{queue_html}

<hr>
<p class="meta">Spec: <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/docs/methodology/04-propagation.md">&sect;04 propagation</a> &middot;
Operator runbook: <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/docs/methodology/propagation-runbook.md">propagation-runbook.md</a> &middot;
Bug-class catalog: <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/docs/methodology/bug-class-catalog.md">bug-class-catalog.md</a></p>

</body>
</html>
"""
