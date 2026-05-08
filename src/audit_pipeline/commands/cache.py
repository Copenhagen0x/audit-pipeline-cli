"""`audit-pipeline cache` — inspect and manage the PoC test cache.

Tier 2 #10.

The cache stores PoC verdicts keyed on (engine_sha, hypothesis_id,
poc_bytes_hash). Subsequent runs of an identical PoC on the same engine
SHA hit the cache and skip the cargo-test step (~20-60s saved per hit).

Subcommands:
    list   — show recent cache entries
    flush  — delete cache entries (all, or filtered)
    stats  — summary stats
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline.db import FindingsDB

console = Console()


@click.group(name="cache")
def cache_group() -> None:
    """Manage the PoC test cache."""


@cache_group.command(name="list")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def cache_list(ctx: click.Context, limit: int) -> None:
    """Show recent cache entries (most recently touched first)."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")
    rows = db.list_poc_cache(limit=limit)
    if not rows:
        console.print("[dim]cache is empty[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Engine SHA", width=10)
    table.add_column("Hyp", width=24)
    table.add_column("Hash", width=10)
    table.add_column("Outcome")
    table.add_column("Hits", justify="right")
    table.add_column("Last seen")
    for r in rows:
        table.add_row(
            (r.get("engine_sha") or "")[:10],
            (r.get("hypothesis_id") or "")[:24],
            (r.get("poc_hash") or "")[:10],
            r.get("outcome") or "",
            str(r.get("n_hits") or 1),
            (r.get("last_seen_at") or "")[:19],
        )
    console.print(table)


@cache_group.command(name="flush")
@click.option("--engine-sha", default=None, help="Filter to this engine SHA")
@click.option("--hyp", "--hypothesis-id", "hypothesis_id", default=None,
              help="Filter to this hypothesis id")
@click.option("--all", "all_entries", is_flag=True,
              help="Required to flush ALL entries (no filter)")
@click.pass_context
def cache_flush(
    ctx: click.Context,
    engine_sha: str | None,
    hypothesis_id: str | None,
    all_entries: bool,
) -> None:
    """Delete cache entries. Filters narrow scope; --all confirms deleting all."""
    if not (engine_sha or hypothesis_id or all_entries):
        raise click.ClickException(
            "Specify --engine-sha, --hyp, or --all. Refusing to flush without scope."
        )
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")
    deleted = db.flush_poc_cache(
        engine_sha=engine_sha,
        hypothesis_id=hypothesis_id,
    )
    console.print(f"[yellow]flushed[/yellow] {deleted} cache entr{'y' if deleted == 1 else 'ies'}")


@cache_group.command(name="stats")
@click.pass_context
def cache_stats(ctx: click.Context) -> None:
    """Show cache hit/miss summary."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")
    rows = db.list_poc_cache(limit=10_000)
    if not rows:
        console.print("[dim]cache is empty[/dim]")
        return
    total_entries = len(rows)
    total_hits = sum((r.get("n_hits") or 1) for r in rows)
    by_outcome: dict[str, int] = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        by_outcome[o] = by_outcome.get(o, 0) + 1
    saved_seconds = sum((r.get("elapsed_s") or 0) * max((r.get("n_hits") or 1) - 1, 0) for r in rows)
    console.print(f"Cache entries:     {total_entries:,}")
    console.print(f"Total hits:        {total_hits:,}")
    console.print(f"Estimated time saved: ~{saved_seconds:.0f}s ({saved_seconds/60:.1f} min)")
    console.print("By outcome:")
    for o, n in sorted(by_outcome.items()):
        console.print(f"  {o:25s}: {n}")
