"""CLI entrypoint for audit-pipeline.

Provides the top-level `audit-pipeline` command and dispatches to subcommands.
"""

import sys

import click
from rich.console import Console

from audit_pipeline import __version__
from audit_pipeline.commands import (
    cross_check,
    debate,
    disclose,
    freshness,
    hunt,
    init,
    kani,
    litesvm,
    poc,
    propagate,
    provision_vps,
    recon,
    run,
    shadow,
    spec_check,
    sync,
    synth_kani,
    watch,
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
    Core layers:
      1. Multi-agent code review (recon)
      2. Empirical PoC (poc)
      3. Kani formal verification (kani)
      4. LiteSVM BPF-level reachability (litesvm)
      5. Cross-platform reproduction (cross-check)

    \b
    Force-multiplier commands:
      Layer 0:   spec-check    (spec ↔ code gap analysis)
      Layer 1.5: debate        (adversarial second-opinion on Layer 1 verdicts)
      Layer 1.6: propagate     (cross-protocol pattern search after a finding)
      Layer 2.5: synth-kani    (NL invariant → Kani harness)
      Layer 6:   shadow start  (live mainnet shadow audit)

    \b
    Live-source tracking (so you never audit stale code):
      freshness  (one-shot: how stale is my workspace vs upstream?)
      watch      (continuous: pull new commits + auto-rerun audit)

    Run `audit-pipeline init` to scaffold a new audit workspace.
    """
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace


# Subcommands — core 5-layer pipeline
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

# Subcommands — force multipliers (the "how the fuck" tier)
main.add_command(spec_check.spec_check_cmd)        # Layer 0
main.add_command(debate.debate_cmd)                # Layer 1.5
main.add_command(propagate.propagate_cmd)          # Layer 1.6 (group: init-corpus, search)
main.add_command(synth_kani.synth_kani_cmd)        # Layer 2.5
main.add_command(shadow.shadow_group)              # Layer 6  (group: start, tail)

# Subcommands — freshness / live-source tracking
main.add_command(freshness.freshness_cmd)          # one-shot staleness check
main.add_command(watch.watch_cmd)                  # continuous source-code watch

# Autonomous hunt loop (the production entry point)
main.add_command(hunt.hunt_cmd)                    # recon -> debate -> PoC -> report


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
