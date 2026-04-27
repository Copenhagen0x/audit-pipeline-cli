"""CLI entrypoint for audit-pipeline.

Provides the top-level `audit-pipeline` command and dispatches to subcommands.
"""

import sys

import click
from rich.console import Console

from audit_pipeline import __version__
from audit_pipeline.commands import (
    cross_check,
    disclose,
    init,
    kani,
    litesvm,
    poc,
    provision_vps,
    recon,
    run,
    sync,
)

console = Console()


@click.group()
@click.version_option(__version__, prog_name="audit-pipeline")
@click.option(
    "--workspace",
    "-w",
    envvar="AUDIT_PIPELINE_WORKSPACE",
    default=".",
    show_default=True,
    help="Path to the audit workspace (created by `init`).",
)
@click.pass_context
def main(ctx: click.Context, workspace: str) -> None:
    """audit-pipeline — orchestrate a Solana security audit through 5 layers.

    \b
    Layers:
      1. Multi-agent code review (recon)
      2. Empirical PoC (poc)
      3. Kani formal verification (kani)
      4. LiteSVM BPF-level reachability (litesvm)
      5. Cross-platform reproduction (cross-check)

    Run `audit-pipeline init` to scaffold a new audit workspace.
    """
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace


# Subcommands
main.add_command(init.init_cmd)
main.add_command(provision_vps.provision_vps_cmd)
main.add_command(sync.sync_cmd)
main.add_command(recon.recon_cmd)
main.add_command(poc.poc_cmd)
main.add_command(kani.kani_cmd)
main.add_command(litesvm.litesvm_cmd)
main.add_command(cross_check.cross_check_cmd)
main.add_command(disclose.disclose_cmd)
main.add_command(run.run_cmd)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
