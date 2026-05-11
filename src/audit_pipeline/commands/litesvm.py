"""`audit-pipeline litesvm` — author + dispatch Layer-4 LiteSVM tests.

Subcommands:
  litesvm author    — instantiate a LiteSVM test from a template
  litesvm dispatch  — run a LiteSVM test on the VPS
"""

import json
import subprocess
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group(name="litesvm")
def litesvm_cmd() -> None:
    """Layer 4 — LiteSVM BPF-level reachability + bound analysis."""


@litesvm_cmd.command(name="author")
@click.option("--finding", "-f", required=True, help="Short snake_case identifier")
@click.option(
    "--template",
    "-t",
    type=click.Choice(["litesvm_reachability_test", "litesvm_bound_analysis"]),
    required=True,
    help="Template type",
)
@click.option(
    "--instruction",
    default=None,
    help="BPF instruction name (for reachability tests)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
@click.pass_context
def litesvm_author(
    ctx: click.Context,
    finding: str,
    template: str,
    instruction: str | None,
    output: Path | None,
) -> None:
    """Author a LiteSVM test from a template."""
    workspace = Path(ctx.obj["workspace"])
    if output is None:
        output = workspace / "tests" / "wrapper"
    output.mkdir(parents=True, exist_ok=True)

    from audit_pipeline import __file__ as pkg_init
    template_path = Path(pkg_init).parent / "templates" / f"{template}.rs.template"
    if not template_path.exists():
        raise click.ClickException(f"Template missing at {template_path}")

    content = template_path.read_text()
    content = content.replace("<FINDING_NAME>", finding)
    if instruction:
        content = content.replace("<INSTRUCTION_NAME>", instruction)

    suffix = "_litesvm" if template == "litesvm_reachability_test" else "_bound_analysis"
    out_path = output / f"test_{finding}{suffix}.rs"
    # FIX C4: idempotent — skip silently if the file already exists. Hunt is
    # invoked in a loop and re-runs across cycles will collide otherwise.
    # Caller can delete the file to force re-authoring.
    if out_path.exists():
        console.print(f"[yellow]{out_path} exists; skipping.[/yellow]")
        return

    out_path.write_text(content)
    console.print(f"[green]Wrote {out_path}[/green]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Edit the test to encode your specific finding's bound analysis")
    console.print(f"  2. Local run: [cyan]cargo test --test test_{finding}{suffix}[/cyan]")
    console.print(f"  3. Cross-platform: [cyan]audit-pipeline cross-check --test test_{finding}{suffix}[/cyan]")


@litesvm_cmd.command(name="dispatch")
@click.option("--test", "-T", required=True, help="LiteSVM test function name")
@click.option("--features", default="", help="Cargo feature flags (e.g. 'small')")
@click.pass_context
def litesvm_dispatch(ctx: click.Context, test: str, features: str) -> None:
    """Run a LiteSVM test on the VPS."""
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException("No workspace.json. Run init first.")

    config = json.loads(config_path.read_text())
    host = config.get("vps", {}).get("host")
    key = config.get("vps", {}).get("ssh_key")
    if not host or not key:
        raise click.ClickException("VPS not configured. Run `audit-pipeline sync` first.")

    from audit_pipeline import __file__ as pkg_init
    script = Path(pkg_init).parent / "scripts" / "dispatch_litesvm.sh"

    feat_arg = f"--features {features}" if features else ""
    args = ["bash", str(script), host, key, test]
    if feat_arg:
        args.append(feat_arg)

    subprocess.run(args, check=False)
