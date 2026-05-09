"""`audit-pipeline triage-siblings` — review and merge LLM-derived sibling hypotheses.

P2 D13. After a confirmed finding, `derive_siblings` writes a YAML of N
structural siblings to `<workspace>/derived/<slug>-siblings.yaml`. Before
those siblings flow into the next hunt cycle as live hypotheses, an
operator should review them: keep what looks structurally sound, drop
duplicates / obvious misses, optionally merge keepers into a canonical
class library.

This module provides four subcommands wrapped under one group:

  list                     show every pending / approved / rejected sibling YAML
  show <finding-id>        print the YAML for one finding to stdout
  approve <finding-id>     mark all siblings approved (file moved to derived/approved/)
  reject  <finding-id>     mark all siblings rejected (file moved to derived/rejected/)
  merge   <finding-id> --target <yaml-path>
                           append approved siblings into a canonical class library

Status tracking is filesystem-only:

  derived/<slug>-siblings.yaml             — pending review (default state)
  derived/approved/<slug>-siblings.yaml    — operator-approved
  derived/rejected/<slug>-siblings.yaml    — operator-rejected
  derived/merged/<slug>-siblings.yaml      — already pasted into class library

No DB rows: review state is intentionally lightweight + git-trackable.

Usage:
    audit-pipeline triage-siblings list
    audit-pipeline triage-siblings show 123
    audit-pipeline triage-siblings approve 123
    audit-pipeline triage-siblings reject 124 --reason "duplicates of SIB-V7-1"
    audit-pipeline triage-siblings merge 123 --target src/audit_pipeline/templates/hypotheses/perp_dex/V7-class.yaml
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from audit_pipeline.db import open_findings_db

console = Console()


# ─────────────────────── Helpers ───────────────────────────


def _slug_for_finding(finding: dict) -> str:
    """Derive the slug used by derive_siblings to name the YAML file."""
    return (finding.get("hypothesis_id") or f"finding-{finding.get('id')}").replace("/", "-")


def _find_sibling_file(workspace: Path, finding_id: int) -> tuple[Path, str] | None:
    """Locate the sibling YAML for a finding; returns (path, status) or None.

    status ∈ {"pending", "approved", "rejected", "merged"}.
    """
    db = open_findings_db(workspace)
    f = db.get_finding(finding_id)
    if not f:
        return None
    slug = _slug_for_finding(f)
    fname = f"{slug}-siblings.yaml"
    derived_dir = workspace / "derived"
    for status, sub in [("approved", "approved"), ("rejected", "rejected"),
                        ("merged", "merged"), ("pending", "")]:
        p = (derived_dir / sub / fname) if sub else (derived_dir / fname)
        if p.is_file():
            return (p, status)
    return None


def _list_yamls(derived_dir: Path) -> dict[str, list[Path]]:
    """Group sibling YAMLs by status."""
    out: dict[str, list[Path]] = {"pending": [], "approved": [], "rejected": [], "merged": []}
    if not derived_dir.is_dir():
        return out
    for p in sorted(derived_dir.glob("*-siblings.yaml")):
        out["pending"].append(p)
    for status in ("approved", "rejected", "merged"):
        sub = derived_dir / status
        if sub.is_dir():
            out[status].extend(sorted(sub.glob("*-siblings.yaml")))
    return out


def _move(src: Path, dst_dir: Path) -> Path:
    """Move src into dst_dir (created if missing). Returns final path."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.is_file():
        dst.unlink()
    shutil.move(str(src), str(dst))
    return dst


# ─────────────────────── CLI ───────────────────────────


@click.group(name="triage-siblings")
def triage_siblings_cmd() -> None:
    """Review LLM-derived sibling hypotheses before they enter live hunts (P2 D13)."""


@triage_siblings_cmd.command(name="list")
@click.pass_context
def cmd_list(ctx: click.Context) -> None:
    """Show every sibling YAML grouped by review status."""
    workspace = Path(ctx.obj["workspace"])
    derived_dir = workspace / "derived"
    groups = _list_yamls(derived_dir)
    total = sum(len(v) for v in groups.values())
    if total == 0:
        console.print(f"[dim]no sibling YAMLs under {derived_dir}[/dim]")
        return

    for status in ("pending", "approved", "rejected", "merged"):
        files = groups[status]
        if not files:
            continue
        console.print(f"\n[bold]{status.upper()} ({len(files)})[/bold]")
        t = Table(show_header=True, header_style="bold")
        t.add_column("Slug")
        t.add_column("Path", overflow="fold")
        for p in files:
            slug = p.stem.removesuffix("-siblings")
            t.add_row(slug, str(p.relative_to(workspace)))
        console.print(t)


@triage_siblings_cmd.command(name="show")
@click.argument("finding_id", type=int)
@click.pass_context
def cmd_show(ctx: click.Context, finding_id: int) -> None:
    """Print the sibling YAML for a finding to stdout."""
    workspace = Path(ctx.obj["workspace"])
    found = _find_sibling_file(workspace, finding_id)
    if not found:
        raise click.ClickException(
            f"no sibling YAML found for finding {finding_id}"
        )
    path, status = found
    console.print(f"[dim]# status: {status}[/dim]")
    console.print(f"[dim]# path:   {path.relative_to(workspace)}[/dim]")
    console.print(path.read_text(encoding="utf-8"))


@triage_siblings_cmd.command(name="approve")
@click.argument("finding_id", type=int)
@click.pass_context
def cmd_approve(ctx: click.Context, finding_id: int) -> None:
    """Mark a sibling YAML approved (move to derived/approved/)."""
    workspace = Path(ctx.obj["workspace"])
    found = _find_sibling_file(workspace, finding_id)
    if not found:
        raise click.ClickException(f"no sibling YAML for finding {finding_id}")
    path, status = found
    if status == "approved":
        console.print(f"[yellow]already approved:[/yellow] {path.relative_to(workspace)}")
        return
    if status not in ("pending", "rejected"):
        raise click.ClickException(
            f"cannot approve from status {status!r}; only pending/rejected are reviewable"
        )
    new_path = _move(path, workspace / "derived" / "approved")
    console.print(f"[green]approved[/green] {new_path.relative_to(workspace)}")


@triage_siblings_cmd.command(name="reject")
@click.argument("finding_id", type=int)
@click.option("--reason", default=None, help="Optional rejection note (free-text)")
@click.pass_context
def cmd_reject(ctx: click.Context, finding_id: int, reason: str | None) -> None:
    """Mark a sibling YAML rejected (move to derived/rejected/)."""
    workspace = Path(ctx.obj["workspace"])
    found = _find_sibling_file(workspace, finding_id)
    if not found:
        raise click.ClickException(f"no sibling YAML for finding {finding_id}")
    path, status = found
    if status == "rejected":
        console.print(f"[yellow]already rejected:[/yellow] {path.relative_to(workspace)}")
        return
    if status not in ("pending", "approved"):
        raise click.ClickException(
            f"cannot reject from status {status!r}"
        )
    new_path = _move(path, workspace / "derived" / "rejected")
    if reason:
        # Sidecar reason file so future re-review has context
        (new_path.with_suffix(".reason.txt")).write_text(reason, encoding="utf-8")
    console.print(f"[red]rejected[/red] {new_path.relative_to(workspace)}")
    if reason:
        console.print(f"  reason: {reason}")


@triage_siblings_cmd.command(name="merge")
@click.argument("finding_id", type=int)
@click.option("--target", required=True, type=click.Path(path_type=Path),
              help="Existing class-library YAML to append the siblings into")
@click.pass_context
def cmd_merge(ctx: click.Context, finding_id: int, target: Path) -> None:
    """Append approved siblings from finding into a canonical class-library YAML.

    After merge, the file moves to derived/merged/ so it's clear this
    set of siblings is no longer pending.
    """
    workspace = Path(ctx.obj["workspace"])
    found = _find_sibling_file(workspace, finding_id)
    if not found:
        raise click.ClickException(f"no sibling YAML for finding {finding_id}")
    path, status = found
    if status != "approved":
        raise click.ClickException(
            f"can only merge from status 'approved'; current status is {status!r}. "
            f"Run `audit-pipeline triage-siblings approve {finding_id}` first."
        )

    if not target.is_file():
        raise click.ClickException(f"target class library not found: {target}")

    # Load both YAMLs, append siblings list under hypotheses key
    src_doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tgt_doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

    src_hyps = src_doc.get("hypotheses") or []
    if not src_hyps:
        raise click.ClickException(f"sibling YAML has no 'hypotheses' list: {path}")

    tgt_hyps = tgt_doc.get("hypotheses") or []
    existing_ids = {h.get("id") for h in tgt_hyps if isinstance(h, dict)}

    appended = 0
    skipped_dup = 0
    for h in src_hyps:
        if not isinstance(h, dict):
            continue
        if h.get("id") in existing_ids:
            skipped_dup += 1
            continue
        tgt_hyps.append(h)
        appended += 1

    tgt_doc["hypotheses"] = tgt_hyps
    target.write_text(yaml.safe_dump(tgt_doc, sort_keys=False), encoding="utf-8")

    new_path = _move(path, workspace / "derived" / "merged")
    console.print(
        f"[green]merged[/green] {appended} sibling(s) → {target} "
        f"(skipped {skipped_dup} dup id(s))"
    )
    console.print(f"  source moved to {new_path.relative_to(workspace)}")
