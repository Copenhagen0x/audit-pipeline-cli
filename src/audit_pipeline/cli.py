"""CLI entrypoint for audit-pipeline.

Provides the top-level `audit-pipeline` command and dispatches to subcommands.
"""

import sys

import click
from rich.console import Console

from audit_pipeline import __version__
from audit_pipeline.commands import (
    cache,
    confirm,
    cross_check,
    customer,
    dashboard,
    debate,
    derive_siblings,
    disclose,
    expand_coverage,
    freshness,
    health,
    heartbeat,
    hunt,
    hunt_deep,
    init,
    issue,
    kani,
    learn,
    litesvm,
    narrative,
    notify,
    onboard,
    poc,
    propagate,
    provision_vps,
    recon,
    report,
    run,
    scheduler,
    shadow,
    sign,
    spec_check,
    sync,
    synth_kani,
    triage,
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
    """audit-pipeline — orchestrate a continuous Solana security audit (Layers 0–6).

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


# Subcommands — core pipeline (Layers 0–6)
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
main.add_command(hunt.hunt_cmd)                    # recon -> debate -> PoC -> Kani -> report
main.add_command(hunt_deep.hunt_deep_cmd)          # tool-using deep hunt (read_file, grep, find_function)
main.add_command(learn.learn_cmd)                  # generate hyps from public disclosures
main.add_command(expand_coverage.expand_coverage_cmd)  # generate hyps from spec, kani-gaps, wrapper
main.add_command(derive_siblings.derive_siblings_cmd)  # auto-derive siblings from a confirmed finding (Tier 2 #8)
main.add_command(cache.cache_group)                # PoC test cache (Tier 2 #10): list / flush / stats
main.add_command(triage.triage_cmd)                # Triage UI (Tier 2 #12): local web UI for new-findings backlog
main.add_command(confirm.confirm_cmd)              # empirical PoC: write custom test, cargo test, report

# Subcommands — commercial layer (T1: severity + lifecycle + reports + issues)
main.add_command(issue.issue_cmd)                  # gh issue draft / file / auto-file
main.add_command(report.report_cmd)                # HTML cycle + weekly reports

# Subcommands — multi-customer layer (T2)
main.add_command(onboard.onboard_cmd)              # one-shot customer onboarding
main.add_command(dashboard.dashboard_cmd)          # HTML status dashboard

# Subcommands — operational hardening (T3)
main.add_command(health.health_cmd)                # daemon health check (systemd timer)

# Subcommands — finding-quality layer (narrative writeups + signed disclosures)
main.add_command(narrative.narrative_cmd)          # LLM narrative writeups for findings
main.add_command(sign.sign_cmd)                    # Ed25519-signed disclosures

# Subcommands — customer-facing notifications + cadence dispatch (Sprint 3.3)
main.add_command(notify.notify_cmd)                # email: test/critical/cadence
main.add_command(scheduler.scheduler_cmd)          # cadence daemon: tick/run/status

# Subcommands — multi-tenant + proof-of-running (Tier 5)
main.add_command(customer.customer_cmd)            # Tier 5 #26 + #27 + #28: customer registry + derived keys
main.add_command(heartbeat.heartbeat_cmd)          # Tier 5 #29: signed proof-of-running heartbeat


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
