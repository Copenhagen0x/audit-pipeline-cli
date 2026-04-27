"""`audit-pipeline cross-check` — Layer-5 cross-platform reproduction."""

import json
import subprocess
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.command(name="cross-check")
@click.option("--test", "-T", required=True, help="Test function name to compare")
@click.option("--features", default="", help="Cargo feature flags")
@click.option(
    "--target",
    type=click.Choice(["engine", "wrapper"]),
    default="wrapper",
    show_default=True,
    help="Which repo the test lives in",
)
@click.pass_context
def cross_check_cmd(ctx: click.Context, test: str, features: str, target: str) -> None:
    """Diff a test's output between local and VPS.

    Confirms the test produces bit-identical output on both platforms,
    catching platform-specific artifacts before disclosure.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException("No workspace.json. Run init first.")

    config = json.loads(config_path.read_text())
    host = config.get("vps", {}).get("host")
    key = config.get("vps", {}).get("ssh_key")
    if not host or not key:
        raise click.ClickException("VPS not configured. Run sync first.")

    repo_local_path = workspace / config[target]["local"]

    from audit_pipeline import __file__ as pkg_init
    script = Path(pkg_init).parent / "scripts" / "cross_platform_compare.sh"

    args = [
        "bash", str(script),
        host, key,
        str(repo_local_path),
        test,
    ]
    if features:
        args.append(f"--features {features}")

    result = subprocess.run(args, check=False)
    if result.returncode != 0:
        # 1 = divergent (intentional fail); >1 = real error
        if result.returncode == 1:
            console.print(
                "[yellow]Outputs diverged between local and VPS. "
                "Investigate before disclosing the finding.[/yellow]"
            )
        raise click.ClickException(f"cross_platform_compare.sh exited {result.returncode}")
