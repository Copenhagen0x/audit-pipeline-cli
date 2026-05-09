"""`audit-pipeline merkle` — per-cycle Merkle root computation (P4 Y0).

Subcommands:
  compute <cycle-id>   Compute + write merkle.json sidecar for one cycle
  verify <cycle-id>    Recompute and compare against the on-disk merkle.json
                       (tamper detection)
  list                 Show recent cycles + their stored Merkle roots
  rebuild-all          Compute merkle.json for every cycle that doesn't have one
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline.db import open_findings_db
from audit_pipeline.merkle import (
    SCHEMA_VERSION,
    cycle_merkle_root,
    cycle_merkle_summary,
)

console = Console()


def _ws(ctx: click.Context) -> Path:
    return Path(ctx.obj["workspace"])


def _merkle_path(workspace: Path, cycle_id: str) -> Path:
    """Sidecar location: <workspace>/hunts/<cycle-id>/merkle.json."""
    return workspace / "hunts" / cycle_id / "merkle.json"


def _findings_for_cycle(db, cycle_id: str) -> list[dict]:
    """Targeted DB query — avoids silent truncation that list_findings has."""
    return db.list_findings_by_cycle(cycle_id)


def _cycle_record(db, cycle_id: str) -> dict | None:
    for c in db.list_cycles(limit=10_000):
        if c.get("cycle_id") == cycle_id:
            return c
    return None


@click.group(name="merkle")
def merkle_cmd() -> None:
    """Per-cycle Merkle root: tamper-evident summary, on-chain ready (P4 Y0)."""


@merkle_cmd.command(name="compute")
@click.argument("cycle_id", type=str)
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Output path (default: <workspace>/hunts/<cycle-id>/merkle.json)")
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Auto-sign the merkle.json with the workspace Ed25519 key")
@click.pass_context
def compute_cmd(ctx: click.Context, cycle_id: str, out: Path | None, sign: bool) -> None:
    """Compute + persist the Merkle root for one cycle."""
    workspace = _ws(ctx)
    db = open_findings_db(workspace)
    cycle = _cycle_record(db, cycle_id)
    if not cycle:
        raise click.ClickException(f"cycle {cycle_id} not found in DB")
    findings = _findings_for_cycle(db, cycle_id)

    summary = cycle_merkle_summary(cycle, findings)
    out = out or _merkle_path(workspace, cycle_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")
    console.print(f"  root: {summary['merkle_root']}")
    console.print(f"  leaves: {summary['n_leaves']} (1 cycle + {summary['n_findings']} findings)")

    if sign:
        try:
            from audit_pipeline.commands.sign import default_key_path, sign_file
            key = default_key_path(workspace)
            if key.is_file():
                sig_path = sign_file(out, key)
                console.print(f"[green]signed[/green] {sig_path}")
            else:
                console.print(f"[dim]signing skipped — no key at {key}[/dim]")
        except Exception as e:
            console.print(f"[yellow]signing failed:[/yellow] {e}")


@merkle_cmd.command(name="verify")
@click.argument("cycle_id", type=str)
@click.pass_context
def verify_cmd(ctx: click.Context, cycle_id: str) -> None:
    """Recompute the root and compare against the on-disk merkle.json.

    Exit non-zero if mismatch — that means either the DB or the
    merkle.json was modified since `compute` last ran.
    """
    workspace = _ws(ctx)
    sidecar = _merkle_path(workspace, cycle_id)
    if not sidecar.is_file():
        raise click.ClickException(
            f"no merkle.json at {sidecar} — run `merkle compute {cycle_id}` first"
        )
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    saved_root = saved.get("merkle_root", "")
    saved_schema = saved.get("schema", "")
    expected_schema = f"jelleo-cycle-merkle-{SCHEMA_VERSION}"

    # Schema-rotation check: if the sidecar's schema doesn't match the
    # current code's schema, the mismatch is by design (FINDING_FIELDS
    # was extended) — surface that distinctly from "tamper detected".
    if saved_schema and saved_schema != expected_schema:
        console.print(
            f"[yellow]SCHEMA ROTATED[/yellow] sidecar={saved_schema} "
            f"current={expected_schema}"
        )
        console.print(
            f"  This sidecar was computed under a previous Merkle schema. "
            f"Re-run `merkle compute {cycle_id}` to regenerate under "
            f"the current schema before re-verifying."
        )
        raise click.ClickException("schema rotated — sidecar predates current code")

    db = open_findings_db(workspace)
    cycle = _cycle_record(db, cycle_id)
    if not cycle:
        raise click.ClickException(f"cycle {cycle_id} not found in DB")
    findings = _findings_for_cycle(db, cycle_id)
    current_root = cycle_merkle_root(cycle, findings)

    if saved_root == current_root:
        console.print(f"[green]OK[/green] root matches: {current_root}")
    else:
        console.print("[red]MISMATCH — DB or merkle.json changed since compute[/red]")
        console.print(f"  saved:   {saved_root}")
        console.print(f"  current: {current_root}")
        raise click.ClickException("merkle root mismatch")


@merkle_cmd.command(name="list")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def list_cmd(ctx: click.Context, limit: int) -> None:
    """Show recent cycles + the on-disk Merkle root (or '-' if missing)."""
    workspace = _ws(ctx)
    db = open_findings_db(workspace)
    cycles = db.list_cycles(limit=limit)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Cycle")
    table.add_column("Engine SHA", width=12)
    table.add_column("Findings", justify="right")
    table.add_column("Merkle root (8 hex)", width=20)
    for c in cycles:
        cid = c.get("cycle_id") or "?"
        sidecar = _merkle_path(workspace, cid)
        root = "-"
        if sidecar.is_file():
            try:
                root = json.loads(sidecar.read_text(encoding="utf-8")).get("merkle_root", "?")[:16]
            except Exception:
                root = "(unreadable)"
        n_f = sum(1 for f in db.list_findings(limit=10_000)
                   if f.get("cycle_id") == cid)
        table.add_row(cid, (c.get("engine_sha") or "?")[:10], str(n_f), root)
    console.print(table)


@merkle_cmd.command(name="rebuild-all")
@click.option("--sign/--no-sign", default=True, show_default=True)
@click.pass_context
def rebuild_all_cmd(ctx: click.Context, sign: bool) -> None:
    """Compute merkle.json for every cycle that doesn't have one."""
    workspace = _ws(ctx)
    db = open_findings_db(workspace)
    cycles = db.list_cycles(limit=10_000)
    n_built, n_skipped = 0, 0
    for c in cycles:
        cid = c.get("cycle_id")
        if not cid:
            continue
        sidecar = _merkle_path(workspace, cid)
        if sidecar.is_file():
            n_skipped += 1
            continue
        findings = _findings_for_cycle(db, cid)
        summary = cycle_merkle_summary(c, findings)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        if sign:
            try:
                from audit_pipeline.commands.sign import default_key_path, sign_file
                key = default_key_path(workspace)
                if key.is_file():
                    sign_file(sidecar, key)
            except Exception:
                pass
        n_built += 1
    console.print(f"[green]built[/green] {n_built} merkle root(s); skipped {n_skipped} existing; "
                   f"schema {SCHEMA_VERSION}")
