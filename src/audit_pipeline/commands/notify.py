"""`audit-pipeline notify` — email notification subcommands.

Three subcommands:
  test       : send a test email through the configured SMTP transport
  critical   : send the immediate alert for a confirmed Critical/High finding
  cadence    : send a scheduled cadence report (24h/weekly/monthly)

Configuration:

  SMTP credentials come from environment variables (preferred — no plaintext
  in the repo):
      JELLEO_SMTP_HOST, JELLEO_SMTP_PORT (default 587),
      JELLEO_SMTP_USER, JELLEO_SMTP_PASSWORD,
      JELLEO_SMTP_FROM (default = SMTP_USER),
      JELLEO_SMTP_TLS (default 'starttls', also: 'ssl' | 'none')

  Recipients are read from the workspace's `notifier.json`:
      {
        "recipients": {
          "critical_oncall":   ["oncall@customer.com"],
          "critical_team":     ["team@customer.com"],
          "cadence_24h":       ["dailies@customer.com"],
          "cadence_weekly":    ["weeklies@customer.com"],
          "cadence_monthly":   ["security-eng@customer.com"]
        }
      }

  Pass --dry-run to render the message without sending — useful for
  previewing the body before pointing at real customer addresses.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import open_findings_db
from audit_pipeline.notifier import (
    NotifierError,
    NotifierSettings,
    send_cadence_report,
    send_critical_alert,
    smtp_test,
)

console = Console()


@click.group(name="notify")
def notify_cmd() -> None:
    """Email notifications and scheduled cadence reports."""


@notify_cmd.command(name="test")
@click.option("--to", required=True, help="Recipient address (one email)")
@click.option("--dry-run", is_flag=True, help="Render the message without SMTP send")
@click.pass_context
def notify_test(ctx: click.Context, to: str, dry_run: bool) -> None:
    """Send a test email through the configured SMTP transport."""
    workspace = Path(ctx.obj["workspace"])
    settings = NotifierSettings.load(workspace, dry_run=dry_run)
    try:
        result = smtp_test(settings, [to])
    except NotifierError as e:
        raise click.ClickException(str(e))
    _print_result(result)


@notify_cmd.command(name="critical")
@click.option("--finding-id", type=int, required=True, help="Confirmed finding to notify on")
@click.option("--repro-link", default="", help="Optional repro URL (PoC, GitHub issue, etc.)")
@click.option("--dry-run", is_flag=True, help="Render the message without SMTP send")
@click.pass_context
def notify_critical(
    ctx: click.Context, finding_id: int, repro_link: str, dry_run: bool,
) -> None:
    """Send the immediate Critical/High alert for a confirmed finding."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    finding = db.get_finding(finding_id)
    if not finding:
        raise click.ClickException(f"Finding {finding_id} not found in DB")

    severity = (finding.get("severity") or "").lower()
    if severity not in {"critical", "high"}:
        raise click.ClickException(
            f"Finding {finding_id} severity is {severity!r}; immediate alert "
            f"is reserved for Critical/High. Use `notify cadence` for the rollup."
        )

    target = next((t for t in db.list_targets() if t["id"] == finding["target_id"]), None)
    target_name = (target or {}).get("name", "(unknown target)")

    settings = NotifierSettings.load(workspace, dry_run=dry_run)
    try:
        result = send_critical_alert(
            settings,
            target_name=target_name,
            finding=finding,
            cycle_id=finding.get("cycle_id", "(unknown)"),
            repro_link=repro_link,
        )
    except NotifierError as e:
        raise click.ClickException(str(e))
    _print_result(result)


@notify_cmd.command(name="cadence")
@click.option(
    "--cadence",
    type=click.Choice(["24h", "weekly", "monthly"]),
    required=True,
)
@click.option("--target", required=True, help="Target name (e.g. percolator)")
@click.option(
    "--report",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the rendered report (HTML or PDF)",
)
@click.option(
    "--sig",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to the report signature (defaults to <report>.sig if it exists)",
)
@click.option("--summary-json", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Optional summary JSON with per-severity counts and window_label")
@click.option("--dry-run", is_flag=True, help="Render the message without SMTP send")
@click.pass_context
def notify_cadence(
    ctx: click.Context,
    cadence: str,
    target: str,
    report: Path,
    sig: Path | None,
    summary_json: Path | None,
    dry_run: bool,
) -> None:
    """Send a 24h / weekly / monthly cadence report to the customer."""
    workspace = Path(ctx.obj["workspace"])
    settings = NotifierSettings.load(workspace, dry_run=dry_run)

    sig_path = sig
    if sig_path is None:
        candidate = report.with_suffix(report.suffix + ".sig")
        if candidate.exists():
            sig_path = candidate

    summary = None
    if summary_json:
        summary = json.loads(summary_json.read_text(encoding="utf-8"))

    try:
        result = send_cadence_report(
            settings,
            cadence=cadence,
            target_name=target,
            report_path=report,
            sig_path=sig_path,
            summary=summary,
        )
    except NotifierError as e:
        raise click.ClickException(str(e))
    _print_result(result)


def _print_result(result: dict) -> None:
    if result.get("dry_run"):
        console.print("[yellow]dry-run[/yellow] — no SMTP send")
        console.print(f"  to:           {result['to']}")
        if result.get("cc"):
            console.print(f"  cc:           {result['cc']}")
        console.print(f"  subject:      {result['subject']}")
        console.print(f"  body chars:   {result['body_text_len']}")
        if result.get("n_attachments"):
            console.print(f"  attachments:  {result['n_attachments']}")
    else:
        console.print(f"[green]sent[/green]  {result['subject']}  →  {result['to']}")
        if result.get("cc"):
            console.print(f"  cc: {result['cc']}")
