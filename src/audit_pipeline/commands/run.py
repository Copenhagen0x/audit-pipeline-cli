"""`audit-pipeline run` — full end-to-end pipeline.

For pre-configured workspaces. Runs all 5 layers in sequence with sane
defaults. For finer control, use individual commands.
"""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

console = Console()


@click.command(name="run")
@click.option("--dry-run", is_flag=True, help="Show what would be run without executing")
@click.pass_context
def run_cmd(ctx: click.Context, dry_run: bool) -> None:
    """Run the full pipeline end-to-end (interactive).

    Walks through:
      1. Confirm workspace + VPS setup
      2. Layer 1: print recon prompt locations (manual agent dispatch)
      3. Layer 2: prompt for each finding to PoC
      4. Layer 3: dispatch Kani harnesses
      5. Layer 4: dispatch LiteSVM tests
      6. Layer 5: cross-platform compare
      7. Generate disclosure docs
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException("No workspace.json. Run `audit-pipeline init` first.")

    config = json.loads(config_path.read_text())

    console.print(Panel.fit(
        f"[bold]audit-pipeline run[/bold]\n\n"
        f"Target:     {config.get('target_name', 'unknown')}\n"
        f"Engine:     {config['engine']['repo']} @ {config['engine']['sha']}\n"
        f"Wrapper:    {config['wrapper']['repo']} @ {config['wrapper']['sha']}\n"
        f"VPS:        {config.get('vps', {}).get('host', '[red]not configured[/red]')}\n",
        title="Pipeline configuration",
    ))

    if dry_run:
        console.print("\n[yellow]Dry run — no commands executed.[/yellow]")
        console.print()
        console.print("Steps that would run:")
        for n, step in enumerate(_pipeline_steps(), start=1):
            console.print(f"  {n}. {step}")
        return

    # Real run: walk through interactively
    console.print()
    console.print("[bold]This is an interactive walkthrough.[/bold]")
    console.print("Press Enter to advance through each layer; Ctrl-C to abort.")
    console.print()

    # Layer 1
    if not click.confirm("Layer 1 — multi-agent recon. Have you written hypotheses.yaml?", default=False):
        console.print("  Build hypotheses.yaml first, then re-run.")
        return
    console.print("  Run: [cyan]audit-pipeline recon --hypotheses hypotheses.yaml[/cyan]")
    console.print("  (then send each prompt in recon/ to your LLM and save responses)")
    click.pause()

    # Layer 2
    console.print()
    console.print("Layer 2 — empirical PoC.")
    console.print("  For each finding: [cyan]audit-pipeline poc -f <name> --engine-function <fn>[/cyan]")
    click.pause()

    # Layer 3
    console.print()
    console.print("Layer 3 — Kani formal verification.")
    console.print(
        "  For each finding/invariant: [cyan]audit-pipeline kani author / dispatch / fetch[/cyan]"
    )
    click.pause()

    # Layer 4
    console.print()
    console.print("Layer 4 — LiteSVM bound + reachability.")
    console.print(
        "  For each finding: [cyan]audit-pipeline litesvm author / dispatch[/cyan]"
    )
    click.pause()

    # Layer 5
    console.print()
    console.print("Layer 5 — cross-platform compare.")
    console.print(
        "  For each test: [cyan]audit-pipeline cross-check --test <name>[/cyan]"
    )
    console.print(
        "  Plus baseline: [cyan]audit-pipeline kani dispatch --baseline[/cyan]"
    )
    click.pause()

    # Disclosure
    console.print()
    console.print("Disclosure scaffolding.")
    console.print("  [cyan]audit-pipeline disclose --findings findings.yaml -o disclosure/[/cyan]")
    click.pause()

    console.print()
    console.print("[bold green]Pipeline walkthrough complete.[/bold green]")
    console.print("  See disclosure/ for the disclosure repo scaffolding.")


def _pipeline_steps() -> list[str]:
    return [
        "Confirm workspace + VPS configured",
        "Layer 1: build prompts from hypotheses.yaml",
        "Layer 1: dispatch agents (manual, your LLM)",
        "Layer 2: instantiate PoC scaffolds for each finding",
        "Layer 2: write witness state, run cargo test",
        "Layer 3: instantiate Kani harnesses",
        "Layer 3: dispatch each harness on VPS via tmux",
        "Layer 3: fetch verdicts + raw logs",
        "Layer 4: instantiate LiteSVM tests",
        "Layer 4: dispatch on VPS",
        "Layer 5: cross-platform compare each test",
        "Layer 5: dispatch maintainer's Kani baseline (capstone)",
        "Generate disclosure docs (README, DISCLOSURE.md, etc.)",
        "Self-audit pass (prompt 12) before publication",
    ]
