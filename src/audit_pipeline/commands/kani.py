"""`audit-pipeline kani` — author + dispatch Layer-3 Kani harnesses.

Subcommands:
  kani author    — instantiate a Kani harness from a template
  kani dispatch  — push a harness to VPS and run via tmux
  kani status    — check status of a running Kani session
  kani fetch     — pull artifacts (log + summary + timings) from VPS
"""

import json
import subprocess
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group(name="kani")
def kani_cmd() -> None:
    """Layer 3 — Kani formal verification orchestration."""


@kani_cmd.command(name="author")
@click.option("--finding", "-f", required=True, help="Short snake_case identifier")
@click.option(
    "--template",
    "-t",
    type=click.Choice(["kani_cex_panic_class", "kani_safe_invariant"]),
    required=True,
    help="Template type",
)
@click.option(
    "--engine-function",
    required=True,
    help="Engine function under verification",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
@click.pass_context
def kani_author(
    ctx: click.Context,
    finding: str,
    template: str,
    engine_function: str,
    output: Path | None,
) -> None:
    """Author a Kani harness from a template."""
    workspace = Path(ctx.obj["workspace"])
    if output is None:
        output = workspace / "tests" / "engine"
    output.mkdir(parents=True, exist_ok=True)

    from audit_pipeline import __file__ as pkg_init
    template_path = Path(pkg_init).parent / "templates" / f"{template}.rs.template"
    if not template_path.exists():
        raise click.ClickException(f"Template missing at {template_path}")

    content = template_path.read_text()
    content = content.replace("<FINDING_NAME>", finding)
    content = content.replace("<INVARIANT_NAME>", finding)
    content = content.replace("<engine_function_name>", engine_function)

    out_path = output / f"proofs_{finding}.rs"
    if out_path.exists():
        raise click.ClickException(f"{out_path} already exists. Refusing to overwrite.")

    out_path.write_text(content)
    console.print(f"[green]Wrote {out_path}[/green]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Edit the harness to bound kani::any() with kani::assume()")
    console.print(f"  2. Dispatch: [cyan]audit-pipeline kani dispatch --harness proof_{finding}[/cyan]")


@kani_cmd.command(name="dispatch")
@click.option("--harness", "-H", required=True, help="Kani harness function name (or --baseline)")
@click.option("--baseline", is_flag=True, help="Run the full baseline (cargo kani --tests)")
@click.pass_context
def kani_dispatch(ctx: click.Context, harness: str, baseline: bool) -> None:
    """Push a Kani harness to VPS and start a tmux session."""
    workspace = Path(ctx.obj["workspace"])
    config = _load_config(workspace)
    host, ssh_key = _vps_or_die(config)

    from audit_pipeline import __file__ as pkg_init
    script = Path(pkg_init).parent / "scripts" / "dispatch_kani.sh"

    arg = "--baseline" if baseline else harness
    result = subprocess.run(
        ["bash", str(script), host, ssh_key, arg],
        check=False,
    )

    if result.returncode != 0:
        raise click.ClickException(f"dispatch_kani.sh exited {result.returncode}")


@kani_cmd.command(name="status")
@click.option("--harness", "-H", required=True, help="Kani harness function name (or --baseline)")
@click.option("--baseline", is_flag=True)
@click.pass_context
def kani_status(ctx: click.Context, harness: str, baseline: bool) -> None:
    """Check status of a running Kani session."""
    workspace = Path(ctx.obj["workspace"])
    config = _load_config(workspace)
    host, ssh_key = _vps_or_die(config)

    from audit_pipeline import __file__ as pkg_init
    script = Path(pkg_init).parent / "scripts" / "check_kani_status.sh"

    arg = "--baseline" if baseline else harness
    subprocess.run(["bash", str(script), host, ssh_key, arg], check=False)


@kani_cmd.command(name="fetch")
@click.option("--harness", "-H", required=True, help="Kani harness function name (or --baseline)")
@click.option("--baseline", is_flag=True)
@click.pass_context
def kani_fetch(ctx: click.Context, harness: str, baseline: bool) -> None:
    """Pull Kani run artifacts (log + summary + timings) from VPS."""
    workspace = Path(ctx.obj["workspace"])
    config = _load_config(workspace)
    host, ssh_key = _vps_or_die(config)

    from audit_pipeline import __file__ as pkg_init
    script = Path(pkg_init).parent / "scripts" / "fetch_kani_artifacts.sh"

    arg = "--baseline" if baseline else harness
    subprocess.run(["bash", str(script), host, ssh_key, arg], check=False)


# ---- helpers ----

def _load_config(workspace: Path) -> dict:
    config_path = workspace / "workspace.json"
    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init` first."
        )
    return json.loads(config_path.read_text())


def _vps_or_die(config: dict) -> tuple[str, str]:
    host = config.get("vps", {}).get("host")
    key = config.get("vps", {}).get("ssh_key")
    if not host or not key:
        raise click.ClickException(
            "VPS not configured in workspace.json. Run `audit-pipeline sync` first."
        )
    return host, key
