"""`audit-pipeline sync` — sync target repos to the VPS at pinned SHAs."""

import json
import subprocess
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.command(name="sync")
@click.option("--host", required=True, help="VPS host (e.g. root@1.2.3.4)")
@click.option("--ssh-key", required=True, type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def sync_cmd(ctx: click.Context, host: str, ssh_key: str) -> None:
    """Clone the target engine + wrapper to the VPS at the pinned SHAs.

    Reads the workspace.json config to determine which repos and SHAs.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init` first."
        )

    config = json.loads(config_path.read_text())
    engine = config["engine"]
    wrapper = config["wrapper"]

    # Locate bundled sync_target_repos.sh
    from audit_pipeline import __file__ as pkg_init
    script = Path(pkg_init).parent / "scripts" / "helpers" / "sync_target_repos.sh"

    if not script.exists():
        raise click.ClickException(f"Bundled sync script missing at {script}")

    console.print(f"[cyan]Syncing to {host}[/cyan]")
    console.print(f"  Engine:  {engine['repo']} @ {engine['sha']}")
    console.print(f"  Wrapper: {wrapper['repo']} @ {wrapper['sha']}")

    result = subprocess.run(
        [
            "bash", str(script),
            host, ssh_key,
            engine["repo"], engine["sha"],
            wrapper["repo"], wrapper["sha"],
        ],
        check=False,
    )

    if result.returncode != 0:
        raise click.ClickException(f"sync_target_repos.sh exited {result.returncode}")

    # Update workspace config with VPS info
    config["vps"]["host"] = host
    config["vps"]["ssh_key"] = ssh_key
    config_path.write_text(json.dumps(config, indent=2))

    console.print(f"[green]Sync complete. VPS info saved to {config_path}.[/green]")
