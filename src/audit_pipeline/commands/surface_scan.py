"""`audit-pipeline surface-scan` — generate a hypotheses.yaml from L1 surface coverage.

Runs the new L1 layer end to end on a target repo (step 1 find bug-spots -> step 2 label ->
step 3 drop provably-safe -> step 4 cheap-model false-alarm filter -> step 5 synthesize) and
writes a loader-ready hypotheses.yaml. That file feeds the existing pipeline unchanged
(`audit-pipeline hunt --hypotheses <file>` or `hunt --surface-scan <repo>`), so deep checking
runs on the complete auto-generated list instead of a hand-capped one.

Coverage-safe: if the run is not fully complete (parse gaps, LLM unavailable, etc.) the command
still writes what it has but prints the COVERAGE_INCOMPLETE/UNKNOWN status and exits non-zero so
a caller never mistakes a partial list for a clean full inventory.
"""
from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

console = Console()


@click.command(name="surface-scan")
@click.option("--repo", "-r", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Path to the target repository to scan.")
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the hypotheses.yaml here (default: <repo>/hypotheses.l1.yaml).")
@click.option("--no-filter", is_flag=True, default=False,
              help="Skip the step-4 cheap-model false-alarm filter (deterministic, no LLM cost; "
                   "more candidates survive).")
@click.pass_context
def surface_scan_cmd(ctx: click.Context, repo: Path, out: Path | None, no_filter: bool) -> None:
    """Generate a hypotheses.yaml from L1 surface coverage of REPO."""
    from audit_pipeline.l1.synthesize import synthesize_repo, write_yaml

    root = repo
    # Default the output to the OPERATOR's cwd, never inside the (untrusted) scanned repo — writing
    # into attacker-controlled directories is how a planted symlink/junction on an ancestor could
    # redirect the write. An explicit --out is the operator's own (trusted) choice.
    out_path = out if out else (Path.cwd() / "hypotheses.l1.yaml")

    # escape() repo paths and L1 notes: they can carry attacker-derived names with Rich markup
    # metacharacters ([ ]) that would otherwise garble the console output.
    console.print(f"[cyan]L1 surface-scan: {escape(str(root))}[/cyan]")
    rep = synthesize_repo(root, run_filter=not no_filter)
    s = rep.summary()

    try:
        written = write_yaml(rep, out_path)
    except OSError as e:
        raise click.ClickException(f"could not write hypotheses to {out_path}: {e}") from e
    console.print(
        f"  status=[bold]{escape(str(s['status']))}[/bold] "
        f"hypotheses={s['raw_hypothesis_count']} "
        f"(kept={s['raw_kept']}, dups_collapsed={s['duplicates_collapsed']}) "
        f"spend=${float(s['spend_usd'] or 0.0):.4f}"
    )
    console.print(f"  wrote -> {escape(str(written))}")
    for note in s["notes"][:20]:
        console.print(f"    [dim]- {escape(str(note)[:500])}[/dim]")  # cap a pathologically long note

    if not rep.hypotheses:
        # zero hypotheses: loud, whether or not coverage was 'complete' — a clean-looking empty
        # set must never read as a successful audit-ready inventory.
        console.print("[yellow]0 hypotheses generated — nothing to hand to the scanner. "
                      "Check the language/parse notes above before trusting this result.[/yellow]")
    if not rep.complete:
        console.print(
            f"[yellow]STATUS {escape(str(s['status']))}: this is NOT a trustworthy complete "
            f"inventory — some surfaces could not be fully evaluated. Review the notes above.[/yellow]"
        )
        ctx.exit(1)
    console.print("[green]OK — complete coverage-safe hypothesis set generated.[/green]")
