"""`audit-pipeline cycle ...` — manage cycle lifecycle state.

Born out of cycle 20260511-183154's retraction. That cycle was killed
manually mid-run during the public-disclosure panic, and nothing in the
pipeline cleaned up its DB row. The result: ``finished_at`` stayed NULL,
the snapshot.json serializer kept reporting ``in_progress: true``, and
the Bridge dashboard's stage line said "Cycle in progress" for a cycle
that had been dead and retracted for hours.

This subcommand group lets the operator close out cycles after the fact:

  audit-pipeline cycle list                   show recent cycles + status
  audit-pipeline cycle finish <cycle_id>      mark finished_at = now (or
                                              the cycle's started_at +
                                              some duration if specified)
  audit-pipeline cycle retract <cycle_id>     finish + write a
                                              retraction.json sidecar
                                              under the cycle dir
                                              (consumed by the cycle
                                              archive page → "RETRACTED"
                                              badge)

The `retract` subcommand is for the next time this happens. It does what
the user had to do by hand yesterday: finish the cycle row AND drop a
``retraction.json`` next to the cycle's signed artefacts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline.db import open_findings_db

console = Console()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@click.group(name="cycle")
def cycle_cmd() -> None:
    """Manage cycle lifecycle state (finish / retract / list)."""


@cycle_cmd.command(name="list")
@click.option("--limit", type=int, default=20, show_default=True)
@click.pass_context
def list_cmd(ctx: click.Context, limit: int) -> None:
    """Show recent cycles + their finished_at / status."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    cycles = db.list_cycles(limit=limit)
    table = Table(title=f"Recent {len(cycles)} cycle(s)")
    table.add_column("Cycle id")
    table.add_column("Started")
    table.add_column("Finished")
    table.add_column("Status", style="bold")
    table.add_column("Dispatched", justify="right")
    table.add_column("Confirmed", justify="right")
    for c in cycles:
        cid = c.get("cycle_id") or "?"
        started = (c.get("started_at") or "")[:19]
        finished = (c.get("finished_at") or "")[:19]
        if c.get("finished_at"):
            status = "[green]finished[/green]"
        else:
            status = "[yellow]in_progress[/yellow]"
        table.add_row(
            cid, started, finished or "—", status,
            str(c.get("n_dispatched") or 0),
            str(c.get("n_confirmed") or 0),
        )
    console.print(table)


@cycle_cmd.command(name="finish")
@click.argument("cycle_id", type=str)
@click.option("--finished-at", default=None,
              help="ISO8601 timestamp (default: now UTC). For cycles "
                   "killed mid-run yesterday, prefer the actual stop "
                   "time so reports show the right duration.")
@click.pass_context
def finish_cmd(ctx: click.Context, cycle_id: str, finished_at: str | None) -> None:
    """Mark a cycle's finished_at without disturbing dispatched/confirmed/cost.

    Use this when a cycle was killed mid-run and nothing in the pipeline
    cleaned up its DB row. The snapshot serializer treats finished_at IS
    NULL as "in_progress", which makes the dashboard misleading.
    """
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    existing = db.get_cycle(cycle_id)
    if not existing:
        raise click.ClickException(f"no cycle with id '{cycle_id}'")
    if existing.get("finished_at"):
        console.print(
            f"[yellow]cycle {cycle_id} is already finished at "
            f"{existing['finished_at']} — nothing to do[/yellow]"
        )
        return
    ok = db.mark_cycle_finished(cycle_id, finished_at=finished_at)
    if not ok:
        raise click.ClickException("update failed (race condition?)")
    refreshed = db.get_cycle(cycle_id)
    console.print(
        f"[green]marked finished[/green] cycle [cyan]{cycle_id}[/cyan] "
        f"at {refreshed.get('finished_at')}"
    )


@cycle_cmd.command(name="retract")
@click.argument("cycle_id", type=str)
@click.option("--reason", required=True,
              help="Short retraction reason (e.g. 'False positives, "
                   "reviewer LGopus debunked all 20 findings').")
@click.option("--retracted-at", default=None,
              help="ISO8601 timestamp the retraction was filed "
                   "(default: now UTC).")
@click.option("--ref-url", default=None,
              help="Optional URL to the retraction post / closed issue.")
@click.pass_context
def retract_cmd(
    ctx: click.Context, cycle_id: str, reason: str,
    retracted_at: str | None, ref_url: str | None,
) -> None:
    """Mark a cycle finished AND write a retraction.json sidecar.

    The retraction.json is read by ``deploy/regen_cycles_index.py`` and
    flips the cycle's row in the public archive to a red "RETRACTED"
    badge. Customers + reviewers see the retracted state instead of
    discovering it via Twitter.
    """
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    existing = db.get_cycle(cycle_id)
    if not existing:
        raise click.ClickException(f"no cycle with id '{cycle_id}'")

    # 1. Finish the DB row if it isn't already
    if not existing.get("finished_at"):
        db.mark_cycle_finished(cycle_id, finished_at=retracted_at)
        console.print(f"  [green]finished[/green] DB row for {cycle_id}")
    else:
        console.print(f"  [dim]DB row already finished at {existing['finished_at']}[/dim]")

    # 2. Write retraction.json into the cycle dir
    cycle_dir = workspace / "hunts" / cycle_id
    if not cycle_dir.is_dir():
        console.print(
            f"[yellow]cycle dir {cycle_dir} not found — DB row updated "
            f"but no archive sidecar written[/yellow]"
        )
        return
    payload = {
        "cycle_id":      cycle_id,
        "retracted_at":  retracted_at or _now_iso(),
        "reason":        reason,
        "ref_url":       ref_url,
        "schema":        "jelleo-retraction-v1",
    }
    sidecar = cycle_dir / "retraction.json"
    sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    console.print(f"  [green]wrote[/green] {sidecar}")
    console.print(
        f"[bold green]Cycle {cycle_id} retracted.[/bold green] "
        f"Run [cyan]regen_cycles_index.py[/cyan] on the VPS to refresh "
        f"the public archive page."
    )


__all__ = ["cycle_cmd"]
