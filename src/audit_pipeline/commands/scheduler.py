"""`audit-pipeline scheduler` — cadence daemon.

Runs report generation + email notifications on three cadences:
  * 24h     — daily rollup at 09:00 UTC (configurable)
  * weekly  — Monday 09:00 UTC (configurable)
  * monthly — first day of month 09:00 UTC (configurable)

The daemon polls every 60s by default and dispatches when the wall clock
crosses a cadence boundary AND the corresponding cadence has not already
fired in this window. State is persisted to `<workspace>/scheduler.state.json`
so a restart does not double-fire a recently-completed cadence.

Per cadence trigger:
  1. For each target in the workspace findings DB, run the corresponding
     `audit-pipeline report` subcommand to render the rollup HTML.
  2. Auto-sign the rollup (already wired into `report` via Sprint 3.2).
  3. Build a per-target summary dict (severity counts, cycle counts, window).
  4. Send `audit-pipeline notify cadence` for each (target, cadence) pair.

This is a stdlib-only loop — no APScheduler, no Celery. For production
deployments the recommended pattern is a systemd timer that invokes
`audit-pipeline scheduler tick --cadence <c>` rather than running the
long-lived `scheduler run` daemon. Both modes are supported.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB
from audit_pipeline.notifier import NotifierError, NotifierSettings, send_cadence_report
from audit_pipeline.severity import Severity

console = Console()


CADENCES = ("24h", "weekly", "monthly")


@click.group(name="scheduler")
def scheduler_cmd() -> None:
    """Cadence daemon: 24h / weekly / monthly report dispatch."""


@scheduler_cmd.command(name="tick")
@click.option(
    "--cadence",
    type=click.Choice(list(CADENCES)),
    required=True,
    help="Which cadence to fire (24h / weekly / monthly)",
)
@click.option("--dry-run", is_flag=True, help="Render reports + emails without SMTP send")
@click.option("--force", is_flag=True, help="Fire even if the cadence already ran in this window")
@click.pass_context
def scheduler_tick(
    ctx: click.Context, cadence: str, dry_run: bool, force: bool,
) -> None:
    """Fire a single cadence pass for every target in the workspace.

    Designed to be invoked by systemd timers (one timer per cadence) so the
    daemon does not need to be long-running. Idempotent within a window
    unless --force is passed.
    """
    workspace = Path(ctx.obj["workspace"])
    state = _load_state(workspace)

    if not force and _already_fired_in_window(state, cadence):
        console.print(
            f"[yellow]scheduler tick[/yellow] {cadence}: already fired this window. "
            f"Pass --force to override."
        )
        return

    db = FindingsDB(workspace / "findings.db")
    targets = db.list_targets()
    if not targets:
        console.print(f"[yellow]scheduler tick[/yellow] {cadence}: no targets in DB.")
        return

    settings = NotifierSettings.load(workspace, dry_run=dry_run)
    fired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_succeeded = n_failed = 0

    for target in targets:
        result = _fire_cadence_for_target(
            workspace=workspace,
            db=db,
            settings=settings,
            target=target,
            cadence=cadence,
            dry_run=dry_run,
        )
        if result.get("ok"):
            n_succeeded += 1
            console.print(
                f"[green]ok[/green] {target['name']:20s} {cadence}  "
                f"→  report={result['report_path']}"
            )
        else:
            n_failed += 1
            console.print(
                f"[red]fail[/red] {target['name']:20s} {cadence}  "
                f"→  {result.get('reason')}"
            )

    state.setdefault("last_fired", {})[cadence] = fired_at
    state.setdefault("history", []).append({
        "cadence": cadence,
        "fired_at": fired_at,
        "n_targets": len(targets),
        "n_succeeded": n_succeeded,
        "n_failed": n_failed,
        "dry_run": dry_run,
    })
    _save_state(workspace, state)

    console.print()
    console.print(
        f"[bold]scheduler tick complete[/bold] · cadence={cadence} · "
        f"fired={n_succeeded}/{n_succeeded + n_failed} · dry_run={dry_run}"
    )


@scheduler_cmd.command(name="run")
@click.option("--poll-sec", type=int, default=60, show_default=True)
@click.option("--anchor-hour-utc", type=int, default=9, show_default=True,
              help="UTC hour at which 24h/weekly/monthly cadences fire (0-23)")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def scheduler_run(
    ctx: click.Context, poll_sec: int, anchor_hour_utc: int, dry_run: bool,
) -> None:
    """Run the long-lived cadence daemon (alternative to systemd timers).

    Polls every poll_sec seconds. Fires each cadence at most once per
    natural window:
      * 24h:     once per day, at the next anchor_hour_utc boundary
      * weekly:  once per week, on Monday at anchor_hour_utc
      * monthly: once per month, on day 1 at anchor_hour_utc

    Press Ctrl-C to stop. Daemon state survives restart via scheduler.state.json.
    """
    workspace = Path(ctx.obj["workspace"])
    console.print(
        f"[bold]scheduler run[/bold] · workspace={workspace} · "
        f"anchor={anchor_hour_utc:02d}:00 UTC · poll={poll_sec}s · dry_run={dry_run}"
    )
    console.print("Ctrl-C to stop.")

    while True:
        try:
            now = datetime.now(timezone.utc)
            state = _load_state(workspace)
            for cadence in CADENCES:
                if not _is_due(now, cadence, anchor_hour_utc, state):
                    continue
                console.print()
                console.print(
                    f"[cyan]{now.isoformat(timespec='seconds')}[/cyan] firing {cadence}"
                )
                ctx.invoke(scheduler_tick, cadence=cadence, dry_run=dry_run, force=False)
            time.sleep(max(15, poll_sec))
        except KeyboardInterrupt:
            console.print("\n[yellow]scheduler run stopped[/yellow]")
            break


@scheduler_cmd.command(name="status")
@click.pass_context
def scheduler_status(ctx: click.Context) -> None:
    """Show last-fired timestamps and recent history."""
    workspace = Path(ctx.obj["workspace"])
    state = _load_state(workspace)
    last = state.get("last_fired", {})
    console.print("[bold]Last fired[/bold]")
    for cadence in CADENCES:
        ts = last.get(cadence, "(never)")
        console.print(f"  {cadence:10s} {ts}")
    history = state.get("history", [])[-10:]
    if history:
        console.print()
        console.print(f"[bold]Recent ticks[/bold] (last {len(history)})")
        for h in reversed(history):
            console.print(
                f"  {h['fired_at']:25s} {h['cadence']:8s} "
                f"ok={h['n_succeeded']}/{h['n_succeeded']+h['n_failed']} "
                f"dry_run={h['dry_run']}"
            )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _state_path(workspace: Path) -> Path:
    return workspace / "scheduler.state.json"


def _load_state(workspace: Path) -> dict[str, Any]:
    p = _state_path(workspace)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(workspace: Path, state: dict[str, Any]) -> None:
    p = _state_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _already_fired_in_window(state: dict[str, Any], cadence: str) -> bool:
    """Return True if the cadence already fired in its current natural window."""
    last_str = (state.get("last_fired") or {}).get(cadence)
    if not last_str:
        return False
    try:
        last = datetime.fromisoformat(last_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    if cadence == "24h":
        return last.date() == now.date()
    if cadence == "weekly":
        # Monday-based ISO week. fired_in_same_iso_week.
        return last.isocalendar()[:2] == now.isocalendar()[:2]
    if cadence == "monthly":
        return (last.year, last.month) == (now.year, now.month)
    return False


def _is_due(
    now: datetime, cadence: str, anchor_hour_utc: int, state: dict[str, Any],
) -> bool:
    """Return True if the cadence should fire at `now`."""
    if _already_fired_in_window(state, cadence):
        return False
    if now.hour < anchor_hour_utc:
        return False
    if cadence == "24h":
        return True
    if cadence == "weekly":
        return now.weekday() == 0  # Monday
    if cadence == "monthly":
        return now.day == 1
    return False


def _window_label(cadence: str, now: datetime) -> str:
    if cadence == "24h":
        start = now - timedelta(days=1)
        return f"{start:%Y-%m-%d %H:%M}–{now:%Y-%m-%d %H:%M} UTC"
    if cadence == "weekly":
        start = now - timedelta(days=7)
        return f"{start:%Y-%m-%d}–{now:%Y-%m-%d}"
    if cadence == "monthly":
        start = now - timedelta(days=30)
        return f"{start:%Y-%m-%d}–{now:%Y-%m-%d}"
    return f"{now:%Y-%m-%d}"


def _summarize_findings(findings: list[dict[str, Any]]) -> dict[str, int]:
    out = {f"n_{s.value.lower()}": 0 for s in Severity}
    for f in findings:
        sev = (f.get("severity") or "").lower()
        key = f"n_{sev}"
        if key in out:
            out[key] += 1
    return out


def _fire_cadence_for_target(
    *,
    workspace: Path,
    db: FindingsDB,
    settings: NotifierSettings,
    target: dict[str, Any],
    cadence: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Render the cadence report for a single target and email it.

    Uses subprocess to invoke `audit-pipeline report cycle/weekly` so the
    existing report generators are reused as-is. Falls back to scheduler.state
    on dispatch failure.
    """
    target_name = target["name"]
    now = datetime.now(timezone.utc)

    if cadence == "24h":
        days = 1
    elif cadence == "weekly":
        days = 7
    else:
        days = 30

    out_dir = workspace / "reports" / cadence
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{target_name}_{cadence}_{now:%Y%m%d}.html"

    cmd = [
        sys.executable, "-m", "audit_pipeline",
        "--workspace", str(workspace),
        "report", "weekly",
        "--target", target_name,
        "--days", str(days),
        "--output", str(report_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "report_render_timeout"}
    if proc.returncode != 0:
        return {"ok": False, "reason": f"report_render_fail: {proc.stderr.strip()[:200]}"}

    sig_path = report_path.with_suffix(report_path.suffix + ".sig")
    if not sig_path.exists():
        sig_path = None

    findings = [
        f for f in db.list_findings(target_id=target["id"], limit=2000)
        if (f.get("created_at") or "") >= (now - timedelta(days=days)).isoformat()
    ]
    cycles = [
        c for c in db.list_cycles(target_id=target["id"], limit=500)
        if (c.get("started_at") or "") >= (now - timedelta(days=days)).isoformat()
    ]
    summary = {
        "window_label": _window_label(cadence, now),
        "n_findings": len(findings),
        "n_cycles": len(cycles),
        **_summarize_findings(findings),
    }

    try:
        send_cadence_report(
            settings,
            cadence=cadence,
            target_name=target_name,
            report_path=report_path,
            sig_path=sig_path,
            summary=summary,
        )
    except NotifierError as e:
        return {
            "ok": True,  # report rendered + signed; just email failed
            "report_path": str(report_path),
            "sig_path": str(sig_path) if sig_path else None,
            "email_warning": str(e),
        }

    return {
        "ok": True,
        "report_path": str(report_path),
        "sig_path": str(sig_path) if sig_path else None,
    }
