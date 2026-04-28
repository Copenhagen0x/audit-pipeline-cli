"""`audit-pipeline health` — daemon health check.

Inspects:
  - findings.db is readable
  - shadow daemon log has been written to in the last N minutes
  - watch daemon log has been written to in the last N minutes
  - last hunt cycle completed (if any) within the last 24h

If anything is degraded, optionally POST a webhook (HUNT_WEBHOOK_URL).
Designed to run from a systemd timer every 5 minutes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import requests
from rich.console import Console
from rich.table import Table

from audit_pipeline.db import FindingsDB

console = Console()


@click.command(name="health")
@click.option("--shadow-log", type=click.Path(),
              default="shadow/daemon.log", show_default=True,
              help="Shadow daemon log path (relative to workspace)")
@click.option("--watch-log", type=click.Path(),
              default="watch/daemon.log", show_default=True,
              help="Watch daemon log path (relative to workspace)")
@click.option("--shadow-stale-min", type=int, default=10, show_default=True)
@click.option("--watch-stale-min", type=int, default=15, show_default=True)
@click.option("--webhook-url", default=None, envvar="HUNT_WEBHOOK_URL",
              help="Slack/Discord webhook URL for degraded alerts")
@click.option("--silent", is_flag=True,
              help="Only print on degraded state (good for cron)")
@click.pass_context
def health_cmd(
    ctx: click.Context,
    shadow_log: str,
    watch_log: str,
    shadow_stale_min: int,
    watch_stale_min: int,
    webhook_url: str | None,
    silent: bool,
) -> None:
    """Health-check the daemons + findings DB. Exit non-zero on degraded."""
    workspace = Path(ctx.obj["workspace"])
    now = datetime.now(timezone.utc)

    checks: list[dict] = []

    db_path = workspace / "findings.db"
    if db_path.exists():
        try:
            db = FindingsDB(db_path)
            stats = db.stats()
            checks.append({
                "name": "findings_db",
                "status": "ok",
                "detail": f"{stats['n_findings']} findings, {stats['n_cycles']} cycles, {stats['n_targets']} targets",
            })
        except Exception as e:  # noqa: BLE001
            checks.append({"name": "findings_db", "status": "fail", "detail": str(e)[:120]})
    else:
        checks.append({"name": "findings_db", "status": "warn", "detail": "no DB yet (no cycles run?)"})

    for label, rel, stale_min in [
        ("shadow", shadow_log, shadow_stale_min),
        ("watch", watch_log, watch_stale_min),
    ]:
        path = workspace / rel
        if not path.exists():
            checks.append({"name": f"{label}_log", "status": "fail",
                           "detail": f"missing {path}"})
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_min = (now - mtime).total_seconds() / 60
        if age_min > stale_min:
            checks.append({"name": f"{label}_log", "status": "fail",
                           "detail": f"stale ({age_min:.1f} min, threshold {stale_min})"})
        else:
            checks.append({"name": f"{label}_log", "status": "ok",
                           "detail": f"fresh ({age_min:.1f} min)"})

    # Last hunt cycle (informational only)
    if db_path.exists():
        try:
            cycles = db.list_cycles(limit=1)
            if cycles:
                last_started = cycles[0].get("started_at", "?")
                checks.append({"name": "last_hunt_cycle", "status": "ok",
                               "detail": f"{cycles[0]['cycle_id']} started {last_started}"})
            else:
                checks.append({"name": "last_hunt_cycle", "status": "warn",
                               "detail": "no cycles in DB yet"})
        except Exception as e:  # noqa: BLE001
            checks.append({"name": "last_hunt_cycle", "status": "warn", "detail": str(e)[:120]})

    failed = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]

    # Print
    if not silent or failed or warns:
        table = Table(title=f"Sentinel health — {now.isoformat(timespec='seconds')}")
        table.add_column("Check")
        table.add_column("Status", style="bold")
        table.add_column("Detail")
        for c in checks:
            color = {"ok": "green", "warn": "yellow", "fail": "red"}[c["status"]]
            table.add_row(c["name"], f"[{color}]{c['status'].upper()}[/{color}]", c["detail"])
        console.print(table)

    if failed and webhook_url:
        try:
            requests.post(webhook_url, json={
                "text": _format_alert(failed, warns, workspace, now),
            }, timeout=15)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]webhook post failed:[/red] {e}")

    sys.exit(2 if failed else 0)


def _format_alert(failed: list[dict], warns: list[dict], workspace: Path, now: datetime) -> str:
    lines = [
        f"⚠️ *Sentinel HEALTH DEGRADED* — {now.isoformat(timespec='seconds')}",
        f"Workspace: `{workspace}`",
        "",
    ]
    for c in failed:
        lines.append(f"• ❌ *{c['name']}* — {c['detail']}")
    for c in warns:
        lines.append(f"• ⚠️ *{c['name']}* — {c['detail']}")
    return "\n".join(lines)
