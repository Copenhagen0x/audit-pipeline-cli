"""`audit-pipeline provision-vps` — install audit toolchain on a fresh VPS.

Wraps the bash script `scripts/provision_vps.sh` from the methodology repo.
"""

import subprocess
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.command(name="provision-vps")
@click.option(
    "--host",
    required=True,
    help="VPS host (e.g. root@1.2.3.4)",
)
@click.option(
    "--ssh-key",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to SSH private key for the VPS",
)
@click.option(
    "--script-path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to provision_vps.sh (defaults to bundled copy)",
)
def provision_vps_cmd(host: str, ssh_key: str, script_path: str | None) -> None:
    """Install Rust + Solana + Kani + tmux + gh on a fresh Ubuntu 22.04 VPS.

    Total install time: ~15-20 min on a 6-core VPS.
    """
    # Locate the bundled provisioning script
    if script_path:
        script = Path(script_path)
    else:
        # Bundled with the package
        from audit_pipeline import __file__ as pkg_init
        bundled = Path(pkg_init).parent / "scripts" / "provision_vps.sh"
        if not bundled.exists():
            raise click.ClickException(
                f"Bundled provision_vps.sh not found at {bundled}. "
                f"Pass --script-path explicitly."
            )
        script = bundled

    console.print(f"[cyan]Provisioning VPS: {host}[/cyan]")
    console.print(f"  Using script: {script}")
    console.print(f"  Using key:    {ssh_key}")
    console.print()

    # Run the bash script
    result = subprocess.run(
        ["bash", str(script), host, ssh_key],
        check=False,
    )

    if result.returncode != 0:
        raise click.ClickException(
            f"provision_vps.sh exited {result.returncode}. "
            f"Check the output above for the failure point."
        )

    console.print()
    console.print("[bold green]VPS provisioned.[/bold green]")
    console.print(f"  Next step: [cyan]audit-pipeline sync --host {host} --ssh-key {ssh_key}[/cyan]")
