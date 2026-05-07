"""`audit-pipeline onboard` — one-shot customer onboarding.

Takes a GitHub URL (and optional engine subpath / wrapper subpath) and:

  1. Clones the repo into the current workspace
  2. Pins to HEAD SHA
  3. Drops a stub `hypotheses.yaml` based on the protocol type
  4. Registers the target in the findings DB
  5. Updates `targets.yaml` (creates it if missing)
  6. Prints next-step instructions (start watch, run first hunt)

Designed to take a customer from "here's our repo URL" to "we're auditing
you continuously" in one command.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel

from audit_pipeline.db import FindingsDB

console = Console()


@click.command(name="onboard")
@click.argument("github_url")
@click.option("--name", "-n", default=None,
              help="Target name (defaults to repo name)")
@click.option("--engine-subdir", default=".",
              help="Path within the repo where the engine code lives")
@click.option("--wrapper-subdir", default=None,
              help="Optional second subdir for a wrapper program")
@click.option("--program-id", default=None,
              help="On-chain Solana program id for shadow audit (optional)")
@click.option("--hypotheses-template",
              type=click.Choice(["minimal", "perp_dex", "lending", "amm"]),
              default="minimal", show_default=True)
@click.pass_context
def onboard_cmd(
    ctx: click.Context,
    github_url: str,
    name: str | None,
    engine_subdir: str,
    wrapper_subdir: str | None,
    program_id: str | None,
    hypotheses_template: str,
) -> None:
    """Onboard a new audit target in one command."""
    workspace = Path(ctx.obj["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)

    target_name = name or _name_from_url(github_url)
    target_dir = workspace / "targets" / target_name
    target_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = target_dir / "repo"

    # 1. Clone (or pull if exists)
    if repo_dir.exists() and (repo_dir / ".git").exists():
        console.print(f"[yellow]Repo already exists at {repo_dir}, pulling…[/yellow]")
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "--all"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"], check=False)
    else:
        console.print(f"[bold]Cloning[/bold] {github_url} -> {repo_dir}")
        subprocess.run(["git", "clone", github_url, str(repo_dir)], check=True)

    # 2. Pin to HEAD SHA
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # 3. Drop a workspace.json (per-target)
    config = {
        "name": target_name,
        "engine": {
            "repo": github_url,
            "sha": sha,
            "local": str((repo_dir / engine_subdir).relative_to(workspace)),
        },
    }
    if wrapper_subdir:
        config["wrapper"] = {
            "repo": github_url,
            "sha": sha,
            "local": str((repo_dir / wrapper_subdir).relative_to(workspace)),
        }
    if program_id:
        config["program_id"] = program_id

    (target_dir / "workspace.json").write_text(json.dumps(config, indent=2))

    # 4. Stub hypotheses.yaml
    hyp_path = target_dir / "hypotheses.yaml"
    if not hyp_path.exists():
        hyp_path.write_text(_stub_hypotheses(target_name, hypotheses_template))

    # 5. Register in DB
    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(
        name=target_name,
        github_url=github_url,
        engine_repo=github_url,
        wrapper_repo=github_url if wrapper_subdir else None,
        config=config,
    )

    # 6. Update / create targets.yaml at workspace root
    targets_yaml = workspace / "targets.yaml"
    if targets_yaml.exists():
        try:
            data = yaml.safe_load(targets_yaml.read_text()) or {"targets": []}
        except yaml.YAMLError:
            data = {"targets": []}
    else:
        data = {"targets": []}
    existing = [t for t in data.get("targets", []) if t.get("name") != target_name]
    existing.append({
        "name": target_name,
        "engine": config.get("engine"),
        "wrapper": config.get("wrapper") if wrapper_subdir else None,
        "program_id": program_id,
        "hypotheses_file": str(hyp_path.relative_to(workspace)),
        "workspace": str(target_dir.relative_to(workspace)),
    })
    data["targets"] = existing
    targets_yaml.write_text(yaml.safe_dump(data, sort_keys=False))

    console.print()
    console.print(Panel.fit(
        f"[bold green]✓ Onboarded[/bold green]  {target_name}\n\n"
        f"DB target id:    {target_id}\n"
        f"Workspace:       {target_dir}\n"
        f"Repo SHA pin:    {sha[:10]}\n"
        f"Hypotheses:      {hyp_path}\n"
        f"Targets index:   {targets_yaml}\n",
        title="Onboard complete",
    ))

    console.print()
    console.print("Next steps:")
    console.print(f"  1. Edit [cyan]{hyp_path}[/cyan] with protocol-specific invariants")
    console.print(f"  2. Smoke-test:  [cyan]audit-pipeline --workspace {target_dir} hunt --skip-poc[/cyan]")
    console.print(f"  3. Production:  add to systemd watch unit's --on-update")


def _name_from_url(url: str) -> str:
    m = re.search(r"[:/]([^/]+?)(\.git)?$", url)
    return (m.group(1) if m else "target").lower()


def _stub_hypotheses(target_name: str, template: str) -> str:
    if template == "perp_dex":
        return _PERP_DEX_HYPOTHESES.replace("{{TARGET}}", target_name)
    if template == "lending":
        return _LENDING_HYPOTHESES.replace("{{TARGET}}", target_name)
    if template == "amm":
        return _AMM_HYPOTHESES.replace("{{TARGET}}", target_name)
    return _MINIMAL_HYPOTHESES.replace("{{TARGET}}", target_name)


_MINIMAL_HYPOTHESES = """\
# Hypothesis library for {{TARGET}}.
# v1 schema (see docs/HYPOTHESIS_SCHEMA.md). Each hypothesis declares which
# protocols it applies_to, which scope_conditions it requires, and which
# bug_class it generalizes to for cross-protocol propagation.
# Edit these in your protocol's terms before running `hunt`.

hypotheses:

  - id: H1-token-balance-conservation
    class: invariant_property
    severity: Critical
    claim: >
      Total token supply held by the program equals the sum of all
      user balances + protocol-owned reserves at all times.
    applies_to: [{{TARGET}}]
    scope_conditions: []
    bug_class: token-balance-conservation-violation

  - id: H2-authorization-on-privileged-instructions
    class: authorization
    severity: High
    claim: >
      Every instruction that mutates global state requires the admin signer
      OR cannot affect funds belonging to other users.
    applies_to: [{{TARGET}}]
    scope_conditions: []
    bug_class: authorization-bypass

  - id: H3-arithmetic-bounds
    class: arithmetic_overflow
    severity: High
    claim: >
      Every multiplication and division in the math paths is provably
      bounded by the protocol's own constants.
    applies_to: [{{TARGET}}]
    scope_conditions: []
    bug_class: arithmetic-overflow
"""

_PERP_DEX_HYPOTHESES = """\
# Perpetual DEX hypothesis library for {{TARGET}}.
# v1 schema (see docs/HYPOTHESIS_SCHEMA.md). applies_to defaults to the
# perp-DEX cluster (drift, mango, marginfi) so confirmed findings here
# automatically propagate across the cluster.

hypotheses:

  - id: H1-vault-conservation
    class: invariant_property
    severity: Critical
    claim: >
      Vault balance == sum(user collateral) + sum(unrealized PnL) +
      insurance fund balance, at every state transition.
    applies_to: [{{TARGET}}, drift, mango, marginfi]
    scope_conditions: [has_insurance_pool, perpetual_funding]
    bug_class: vault-balance-divergence

  - id: H2-funding-rate-no-self-bias
    class: state_transition
    severity: High
    claim: >
      The funding rate captured in any instruction is computed BEFORE any
      mark-price mutation in the same instruction.
    applies_to: [{{TARGET}}, drift, mango]
    scope_conditions: [perpetual_funding]
    bug_class: funding-rate-self-bias

  - id: H3-liquidation-cash-flow
    class: state_transition
    severity: High
    claim: >
      Liquidation does not transfer collateral to the liquidator beyond
      the configured incentive percentage.
    applies_to: [{{TARGET}}, drift, mango, marginfi]
    scope_conditions: [liquidation_engine]
    bug_class: liquidation-incentive-overpayment

  - id: H4-self-trade-cash-neutral
    class: state_transition
    severity: Medium
    claim: >
      A self-trade (same authority on both sides of a fill) is cash-flow
      neutral up to fees + IM transitions.
    applies_to: [{{TARGET}}, drift, mango]
    scope_conditions: [clob_orderbook, perpetual_funding]
    bug_class: self-trade-cash-flow-violation
"""

_LENDING_HYPOTHESES = """\
# Lending protocol hypothesis library for {{TARGET}}.
# v1 schema (see docs/HYPOTHESIS_SCHEMA.md). applies_to defaults to the
# lending cluster (marginfi, kamino) for cross-protocol propagation.

hypotheses:

  - id: H1-collateralization-invariant
    class: invariant_property
    severity: Critical
    claim: >
      For every borrower account, total collateral value (in USD) >=
      total borrowed value (in USD) * minimum collateral ratio.
    applies_to: [{{TARGET}}, marginfi, kamino]
    scope_conditions: [liquidation_engine, multi_collateral]
    bug_class: undercollateralized-position

  - id: H2-interest-accrual-monotonic
    class: implicit_invariant
    severity: High
    claim: >
      Borrow indices are monotonically non-decreasing across all
      state transitions; supply indices are monotonically non-decreasing.
    applies_to: [{{TARGET}}, marginfi, kamino]
    scope_conditions: []
    bug_class: interest-index-non-monotonic

  - id: H3-liquidation-discount-bounded
    class: arithmetic_overflow
    severity: High
    claim: >
      The liquidation bonus paid to a liquidator cannot exceed
      LIQUIDATION_INCENTIVE_PCT of the seized collateral.
    applies_to: [{{TARGET}}, marginfi, kamino]
    scope_conditions: [liquidation_engine]
    bug_class: liquidation-incentive-overpayment
"""

_AMM_HYPOTHESES = """\
# AMM hypothesis library for {{TARGET}}.
# v1 schema (see docs/HYPOTHESIS_SCHEMA.md). applies_to defaults to the
# AMM cluster (orca, raydium, meteora) for cross-protocol propagation.

hypotheses:

  - id: H1-constant-product-invariant
    class: invariant_property
    severity: Critical
    claim: >
      x * y >= k after every swap, where k is the pool's stored invariant.
    applies_to: [{{TARGET}}, orca, raydium, meteora]
    scope_conditions: [amm_constant_product]
    bug_class: constant-product-invariant-violation

  - id: H2-fee-accounting
    class: state_transition
    severity: High
    claim: >
      Fees credited to LPs equal the difference between input amount and
      input amount net of fee, with no rounding in the LP's favor beyond
      sub-lamport amounts.
    applies_to: [{{TARGET}}, orca, raydium, meteora]
    scope_conditions: [amm_constant_product]
    bug_class: fee-accounting-rounding-asymmetry

  - id: H3-flash-loan-repayment
    class: state_transition
    severity: Critical
    claim: >
      Flash-loan instructions must end with the pool's reserve >= the
      pre-loan reserve + the configured flash-loan fee.
    applies_to: [{{TARGET}}, orca, raydium]
    scope_conditions: [flash_loan]
    bug_class: flash-loan-repayment-bypass
"""
